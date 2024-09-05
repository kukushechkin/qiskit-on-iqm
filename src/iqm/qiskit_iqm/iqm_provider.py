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
"""Qiskit Backend Provider for IQM backends.
"""
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from functools import reduce
from typing import Optional, Union
from uuid import UUID
import warnings

import numpy as np
from qiskit import QuantumCircuit
from qiskit.providers import JobStatus, JobV1, Options

from iqm.iqm_client import Circuit, CircuitCompilationOptions, Instruction, IQMClient, RunRequest
from iqm.iqm_client.util import to_json_dict
from iqm.qiskit_iqm.fake_backends import IQMFakeAdonis
from iqm.qiskit_iqm.iqm_backend import IQMBackendBase
from iqm.qiskit_iqm.iqm_job import IQMJob
from iqm.qiskit_iqm.qiskit_to_iqm import MeasurementKey

try:
    __version__ = version('qiskit-iqm')
except PackageNotFoundError:  # pragma: no cover
    __version__ = 'unknown'
finally:
    del version, PackageNotFoundError


class IQMBackend(IQMBackendBase):
    """Backend for executing quantum circuits on IQM quantum computers.

    Args:
        client: client instance for communicating with an IQM server
        **kwargs: optional arguments to be passed to the parent Backend initializer
    """

    def __init__(self, client: IQMClient, **kwargs):
        architecture = client.get_quantum_architecture()
        super().__init__(architecture, **kwargs)
        self.client: IQMClient = client
        self._max_circuits: Optional[int] = None
        self.name = f'IQM{architecture.name}Backend'

    @classmethod
    def _default_options(cls) -> Options:
        return Options(
            shots=1024,
            calibration_set_id=None,
            circuit_compilation_options=CircuitCompilationOptions(),
            circuit_callback=None,
            timeout_seconds=None,
        )

    @property
    def max_circuits(self) -> Optional[int]:
        """Maximum number of circuits that should be run in a single batch.

        Currently there is no hard limit on the number of circuits that can be executed in a single batch/job.
        However, some libraries like Qiskit Experiments use this property to split multi-circuit computational
        tasks into multiple baches/jobs.

        The default value is ``None``, meaning there is no limit. You can set it to a specific integer
        value to force these libraries to run at most that many circuits in a single batch.
        """
        return self._max_circuits

    @max_circuits.setter
    def max_circuits(self, value: Optional[int]) -> None:
        self._max_circuits = value

    def run(self, run_input: Union[QuantumCircuit, list[QuantumCircuit]], **options) -> IQMJob:
        """Run a quantum circuit or a list of quantum circuits on the IQM quantum computer represented with this class.

        Args:
            run_input (Union[QuantumCircuit, list[QuantumCircuit]]): The circuits to run.
            options: A dictionary of options for the run. The following options are supported:
                timeout_seconds Optional(float): Optional timeout passed to the :class:`IQMJob` in seconds.
                other options: Additional options to be passed to :meth:`create_run_request`.

        Returns:
            IQMJob: The Job from which the results can be obtained once the circuits are executed.
        """

        timeout_seconds = options.pop('timeout_seconds', self.options.timeout_seconds)
        run_request = self.create_run_request(run_input, **options)
        job_id = self.client.submit_run_request(run_request)
        job = IQMJob(self, str(job_id), shots=run_request.shots, timeout_seconds=timeout_seconds)
        job.circuit_metadata = [c.metadata for c in run_request.circuits]
        return job

    def create_run_request(
        self,
        run_input: Union[QuantumCircuit, list[QuantumCircuit]],
        shots: Optional[int] = None,
        calibration_set_id: Optional[Union[str, UUID]] = None,
        circuit_compilation_options: Optional[CircuitCompilationOptions] = None,
        circuit_callback: Optional[Callable] = None,
        **unknown_options,
    ) -> RunRequest:
        """Creates a run request without submitting it for execution.

        This can be used to check what would be submitted for execution by an equivalent call to :meth:`run`.

        Args:
            run_input: same as ``run_input`` for :meth:`run`
            shots: The number of shots for each circuit. If not set, it will use the default from `IQMBackend.options`.
            calibration_set_id: ID of the calibration set to use for the run. If not set, it will use the default from
            `IQMBackend.options`.
            circuit_compilation_options: Compilation options for the circuits as documented in ``iqm-client``.
            If not set, it will use the default from `IQMBackend.options`.
            circuit_callback (Callable): Any callback function that will be called for each circuit before sending
            the circuits to the device. If not set, it will use the default from `IQMBackend.options`.

        Returns:
            The created run request object
        """
        # pylint: disable=too-many-arguments
        if self.client is None:
            raise RuntimeError('Session to IQM client has been closed.')

        circuits = [run_input] if isinstance(run_input, QuantumCircuit) else run_input

        if len(circuits) == 0:
            raise ValueError('Empty list of circuits submitted for execution.')

        if shots is None:
            shots = self.options.shots

        # Catch old iqm-client options
        if 'max_circuit_duration_over_t2' in unknown_options or 'heralding_mode' in unknown_options:
            warnings.warn(
                DeprecationWarning(
                    'max_circuit_duration_over_t2 and heralding_mode are deprecated, please use '
                    + 'circuit_compilation_options instead.'
                )
            )
        if circuit_compilation_options is None:
            circuit_compilation_options = CircuitCompilationOptions(
                max_circuit_duration_over_t2=unknown_options.pop(
                    'max_circuit_duration_over_t2',
                    self.options.circuit_compilation_options.max_circuit_duration_over_t2,
                ),
                heralding_mode=unknown_options.pop(
                    'heralding_mode', self.options.circuit_compilation_options.heralding_mode
                ),
            )

        if unknown_options:
            warnings.warn(f'Unknown backend option(s): {unknown_options}')

        if calibration_set_id is None:
            calibration_set_id = self.options.calibration_set_id

        if calibration_set_id is not None and not isinstance(calibration_set_id, UUID):
            calibration_set_id = UUID(calibration_set_id)

        if circuit_callback:
            circuit_callback(circuits)
        elif self.options.circuit_callback:
            self.options.circuit_callback(circuits)

        circuits_serialized: list[Circuit] = [self.serialize_circuit(circuit) for circuit in circuits]
        used_indices: set[int] = reduce(
            lambda qubits, circuit: qubits.union(set(int(q) for q in circuit.all_qubits())), circuits_serialized, set()
        )
        qubit_mapping = {str(idx): qb for idx, qb in self._idx_to_qb.items() if idx in used_indices}

        return self.client.create_run_request(
            circuits_serialized,
            qubit_mapping=qubit_mapping,
            calibration_set_id=calibration_set_id,
            shots=shots,
            options=circuit_compilation_options,
        )

    def retrieve_job(self, job_id: str) -> IQMJob:
        """Create and return an IQMJob instance associated with this backend with given job id."""
        return IQMJob(self, job_id)

    def close_client(self):
        """Close IQMClient's session with the authentication server. Discard the client."""
        if self.client is not None:
            self.client.close_auth_session()
        self.client = None

    def serialize_circuit(self, circuit: QuantumCircuit) -> Circuit:
        """Serialize a quantum circuit into the IQM data transfer format.

        Serializing is not strictly bound to the native gateset, i.e. some gates that are not explicitly mentioned in
        the native gateset of the backend can still be serialized. For example, the native single qubit gate for IQM
        backend is the 'r' gate, however 'x', 'rx', 'y' and 'ry' gates can also be serialized since they are just
        particular cases of the 'r' gate. If the circuit was transpiled against a backend using Qiskit's transpiler
        machinery, these gates are not supposed to be present. However, when constructing circuits manually and
        submitting directly to the backend, it is sometimes more explicit and understandable to use these concrete
        gates rather than 'r'. Serializing them explicitly makes it possible for the backend to accept such circuits.

        Qiskit uses one measurement instruction per qubit (i.e. there is no measurement grouping concept). While
        serializing we do not group any measurements together but rather associate a unique measurement key with each
        measurement instruction, so that the results can later be reconstructed correctly (see :class:`MeasurementKey`
        documentation for more details).

        Args:
            circuit: quantum circuit to serialize

        Returns:
            data transfer object representing the circuit

        Raises:
            ValueError: circuit contains an unsupported instruction or is not transpiled in general
        """
        # pylint: disable=too-many-branches
        instructions = []
        for operation in circuit.data:
            instruction = operation.operation
            qubits = operation.qubits
            clbits = operation.clbits
            qubit_names = [str(circuit.find_bit(qubit).index) for qubit in qubits]
            if instruction.name == 'r':
                angle_t = float(instruction.params[0] / (2 * np.pi))
                phase_t = float(instruction.params[1] / (2 * np.pi))
                instructions.append(
                    Instruction(name='prx', qubits=qubit_names, args={'angle_t': angle_t, 'phase_t': phase_t})
                )
            elif instruction.name == 'x':
                instructions.append(Instruction(name='prx', qubits=qubit_names, args={'angle_t': 0.5, 'phase_t': 0.0}))
            elif instruction.name == 'rx':
                angle_t = float(instruction.params[0] / (2 * np.pi))
                instructions.append(
                    Instruction(name='prx', qubits=qubit_names, args={'angle_t': angle_t, 'phase_t': 0.0})
                )
            elif instruction.name == 'y':
                instructions.append(Instruction(name='prx', qubits=qubit_names, args={'angle_t': 0.5, 'phase_t': 0.25}))
            elif instruction.name == 'ry':
                angle_t = float(instruction.params[0] / (2 * np.pi))
                instructions.append(
                    Instruction(name='prx', qubits=qubit_names, args={'angle_t': angle_t, 'phase_t': 0.25})
                )
            elif instruction.name == 'cz':
                instructions.append(Instruction(name='cz', qubits=qubit_names, args={}))
            elif instruction.name == 'move':
                instructions.append(Instruction(name='move', qubits=qubit_names, args={}))
            elif instruction.name == 'barrier':
                instructions.append(Instruction(name='barrier', qubits=qubit_names, args={}))
            elif instruction.name == 'measure':
                mk = MeasurementKey.from_clbit(clbits[0], circuit)
                instructions.append(Instruction(name='measure', qubits=qubit_names, args={'key': str(mk)}))
            elif instruction.name == 'id':
                pass
            else:
                raise ValueError(
                    f"Instruction '{instruction.name}' in the circuit '{circuit.name}' is not natively supported. "
                    f'You need to transpile the circuit before execution.'
                )

        try:
            metadata = to_json_dict(circuit.metadata)
        except ValueError:
            warnings.warn(
                f'Metadata of circuit {circuit.name} was dropped because it could not be serialised to JSON.',
            )
            metadata = None

        return Circuit(name=circuit.name, instructions=instructions, metadata=metadata)


class IQMFacadeBackend(IQMBackend):
    """Facade backend for mimicking the execution of quantum circuits on IQM quantum computers. Allows to submit a
     circuit to the IQM server, and if the execution was successful, performs a simulation with a respective IQM noise
     model locally, then returns the simulated results.

    Args:
        client: client instance for communicating with an IQM server
        **kwargs: optional arguments to be passed to the parent Backend initializer
    """

    def __init__(self, client: IQMClient, **kwargs):
        self.fake_adonis = IQMFakeAdonis()
        target_architecture = client.get_quantum_architecture()

        if not self.fake_adonis.validate_compatible_architecture(target_architecture):
            raise ValueError('Quantum architecture of the remote quantum computer does not match Adonis.')

        super().__init__(client, **kwargs)
        self.client = client
        self.name = f'IQMFacade{target_architecture.name}Backend'

    def _validate_no_empty_cregs(self, circuit):
        """Returns True if given circuit has no empty (unused) classical registers, False otherwise."""
        cregs_utilization = dict.fromkeys(circuit.cregs, 0)
        used_cregs = [circuit.find_bit(i.clbits[0]).registers[0][0] for i in circuit.data if len(i.clbits) > 0]
        for creg in used_cregs:
            cregs_utilization[creg] += 1

        if 0 in cregs_utilization.values():
            return False
        return True

    def run(self, run_input: Union[QuantumCircuit, list[QuantumCircuit]], **options) -> JobV1:
        circuits = [run_input] if isinstance(run_input, QuantumCircuit) else run_input
        circuits_validated_cregs: list[bool] = [self._validate_no_empty_cregs(circuit) for circuit in circuits]
        if not all(circuits_validated_cregs):
            raise ValueError(
                'One or more circuits contain unused classical registers. This is not allowed for Facade simulation, '
                'see user guide.'
            )

        iqm_backend_job = super().run(run_input, **options)
        iqm_backend_job.result()  # get and discard results
        if iqm_backend_job.status() == JobStatus.ERROR:
            raise RuntimeError('Remote execution did not succeed.')
        return self.fake_adonis.run(run_input, **options)


class IQMProvider:
    """Provider for IQM backends.

    Args:
        url: URL of the IQM Cortex server

    Keyword Args:
        auth_server_url: URL of the user authentication server, if required by the IQM Cortex server.
            Can also be set in the ``IQM_AUTH_SERVER`` environment variable.
        username: Username, if required by the IQM Cortex server.
            Can also be set in the ``IQM_AUTH_USERNAME`` environment variable.
        password: Password, if required by the IQM Cortex server.
            Can also be set in the ``IQM_AUTH_PASSWORD`` environment variable.
    """

    def __init__(self, url: str, **user_auth_args):  # contains keyword args auth_server_url, username, password
        self.url = url
        self.user_auth_args = user_auth_args

    def get_backend(self, name=None) -> Union[IQMBackend, IQMFacadeBackend]:
        """An IQMBackend instance associated with this provider.

        Args:
            name: optional name of a custom facade backend
        """
        client = IQMClient(self.url, client_signature=f'qiskit-iqm {__version__}', **self.user_auth_args)
        if name == 'facade_adonis':
            return IQMFacadeBackend(client)
        return IQMBackend(client)