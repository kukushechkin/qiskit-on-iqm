# Copyright 2022 Qiskit on IQM developers
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Circuit execution jobs.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Optional, Union
import uuid
import warnings

import numpy as np
from qiskit.providers import JobStatus, JobV1
from qiskit.result import Counts, Result

from iqm.iqm_client import (
    CircuitMeasurementResults,
    HeraldingMode,
    IQMClient,
    JobAbortionError,
    RunRequest,
    RunResult,
    Status,
)
from iqm.qiskit_iqm.qiskit_to_iqm import MeasurementKey


class IQMJob(JobV1):
    """Implementation of Qiskit's job interface to handle circuit execution on an IQM server.

    Args:
        backend: the backend instance initiating this job
        job_id: string representation of the UUID generated by IQM server
        **kwargs: arguments to be passed to the initializer of the parent class
    """

    def __init__(self, backend: 'qiskit_iqm.IQMBackend', job_id: str, **kwargs):  # type: ignore
        super().__init__(backend, job_id=job_id, **kwargs)
        self._result: Union[None, list[tuple[str, list[str]]]] = None
        self._calibration_set_id: Optional[uuid.UUID] = None
        self._request: Optional[RunRequest] = None
        self._client: IQMClient = backend.client
        self.circuit_metadata: Optional[list] = None  # Metadata that was originally associated with circuits by user

    def _format_iqm_results(self, iqm_result: RunResult) -> list[tuple[str, list[str]]]:
        """Convert the measurement results from a batch of circuits into the Qiskit format."""
        if iqm_result.measurements is None:
            raise ValueError(
                f'Cannot format IQM result without measurements. Job status is "{iqm_result.status.value.upper()}"'
            )

        requested_shots = self.metadata.get('shots', iqm_result.metadata.request.shots)
        # If no heralding, for all circuits we expect the same number of shots which is the shots requested by user.
        expect_exact_shots = iqm_result.metadata.request.heralding_mode == HeraldingMode.NONE

        return [
            (circuit.name, self._format_measurement_results(measurements, requested_shots, expect_exact_shots))
            for measurements, circuit in zip(iqm_result.measurements, iqm_result.metadata.request.circuits)
        ]

    @staticmethod
    def _format_measurement_results(
        measurement_results: CircuitMeasurementResults, requested_shots: int, expect_exact_shots: bool = True
    ) -> list[str]:
        """Convert the measurement results from a circuit into the Qiskit format."""
        formatted_results: dict[int, np.ndarray] = {}
        for k, v in measurement_results.items():
            mk = MeasurementKey.from_string(k)
            res = np.array(v, dtype=int)
            if len(v) == 0 and not expect_exact_shots:
                warnings.warn(
                    'Received measurement results containing zero shots. '
                    'In case you are using non-default heralding mode, this could be because of bad calibration.'
                )
                res = np.array([])
            else:
                # in Qiskit each measurement is a separate single-qubit instruction. qiskit-iqm assigns unique
                # measurement key to each such instruction, so only one column is expected per measurement key.
                if res.shape[1] != 1:
                    raise ValueError(f'Measurement result {mk} has the wrong shape {res.shape}, expected (*, 1)')
                res = res[:, 0]

            shots = len(res)
            if expect_exact_shots and shots != requested_shots:
                raise ValueError(f'Expected {requested_shots} shots but got {shots} for measurement result {mk}')

            # group the measurements into cregs, fill in zeros for unused bits
            creg = formatted_results.setdefault(mk.creg_idx, np.zeros((shots, mk.creg_len), dtype=int))
            creg[:, mk.clbit_idx] = res

        # 1. Loop over the registers in the reverse order they were added to the circuit.
        # 2. Within each register the highest index is the most significant, so it goes to the leftmost position.
        return [
            ' '.join(''.join(map(str, res[s, ::-1])) for _, res in sorted(formatted_results.items(), reverse=True))
            for s in range(len(res))
        ]

    def submit(self):
        raise NotImplementedError(
            'You should never have to submit jobs by calling this method. When running circuits through '
            'RemoteIQMBackend, the submission will happen under the hood. The job instance that you get is only for '
            'checking the progress and retrieving the results of the submitted job.'
        )

    def cancel(self) -> bool:
        """Attempt to cancel the job.

        Returns:
            True if the job was cancelled successfully, False otherwise
        """
        try:
            self._client.abort_job(uuid.UUID(self._job_id))
            return True
        except JobAbortionError as e:
            warnings.warn(f'Failed to cancel job: {e}')
            return False

    def result(self) -> Result:
        if not self._result:
            results = self._client.wait_for_results(uuid.UUID(self._job_id))
            self._calibration_set_id = results.metadata.calibration_set_id
            self._request = results.metadata.request
            if results.metadata.timestamps is not None:
                self.metadata['timestamps'] = results.metadata.timestamps.copy()
            self._result = self._format_iqm_results(results)
            # RemoteIQMBackend.run() populates circuit_metadata, so it may be None if method wasn't called in current
            # session. In that case retrieve circuit metadata from RunResult.metadata.request.circuits[n].metadata
            if self.circuit_metadata is None:
                self.circuit_metadata = []
                self.circuit_metadata = [c.metadata for c in results.metadata.request.circuits]

        result_dict = {
            'backend_name': None,
            'backend_version': None,
            'qobj_id': None,
            'job_id': self._job_id,
            'success': True,
            'results': [
                {
                    'shots': len(measurement_results),
                    'success': True,
                    'data': {
                        'memory': measurement_results,
                        'counts': Counts(Counter(measurement_results)),
                        'metadata': self.circuit_metadata[i] if self.circuit_metadata is not None else {},
                    },
                    'header': {'name': name},
                    'calibration_set_id': self._calibration_set_id,
                }
                for i, (name, measurement_results) in enumerate(self._result)
            ],
            'date': date.today().isoformat(),
            'request': self._request,
            'timestamps': self.metadata.get('timestamps'),
        }
        return Result.from_dict(result_dict)

    def status(self) -> JobStatus:
        if self._result:
            return JobStatus.DONE

        result = self._client.get_run_status(uuid.UUID(self._job_id))
        if result.status == Status.PENDING_COMPILATION:
            return JobStatus.QUEUED
        if result.status == Status.PENDING_EXECUTION:
            return JobStatus.RUNNING
        if result.status == Status.READY:
            return JobStatus.DONE
        if result.status == Status.FAILED:
            return JobStatus.ERROR
        if result.status == Status.ABORTED:
            return JobStatus.CANCELLED
        raise RuntimeError(f"Unknown run status '{result.status}'")

    def queue_position(self, refresh: bool = False) -> Optional[int]:
        """Return the position of the job in the server queue.

        Note:
            The position is not yet implemented and this function will always
            return ``None``. The ``refresh`` argument is ignored.

        Args:
            refresh: If ``True``, re-query the server to get the latest value.
                Otherwise return the cached value.

        Returns:
            Position in the queue or ``None`` if position is unknown or not applicable.
        """
        # pylint: disable=unused-argument
        return None

    def error_message(self) -> Optional[str]:
        """Returns the error message if job has failed, otherwise returns None."""
        return self._client.get_run_status(uuid.UUID(self._job_id)).message
