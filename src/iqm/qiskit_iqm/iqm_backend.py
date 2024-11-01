# Copyright 2022-2023 Qiskit on IQM developers
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
"""Qiskit backend for IQM quantum computers.
"""
from __future__ import annotations

from abc import ABC
from copy import copy
import itertools
import re
from typing import Final, Union
from uuid import UUID

from qiskit.circuit import Parameter, Reset
from qiskit.circuit.library import CZGate, IGate, Measure, RGate
from qiskit.providers import BackendV2
from qiskit.transpiler import InstructionProperties, Target

from iqm.iqm_client import (
    DynamicQuantumArchitecture,
    GateImplementationInfo,
    GateInfo,
    QuantumArchitectureSpecification,
)
from iqm.qiskit_iqm.move_gate import MoveGate

IQM_TO_QISKIT_GATE_NAME: Final[dict[str, str]] = {'prx': 'r', 'cz': 'cz'}


def _dqa_from_static_architecture(sqa: QuantumArchitectureSpecification) -> DynamicQuantumArchitecture:
    """Create a dynamic quantum architecture from the given static quantum architecture.

    Since the DQA contains some attributes that are not present in an SQA, they are filled with mock data:

    * Each gate type is given a single mock implementation.
    * Calibration set ID is set to the all-zeros UUID.

    Args:
        sqa: static quantum architecture to replicate
    Returns:
        DQA replicating the properties of ``sqa``
    """
    # NOTE this prefix-based heuristic for identifying the qubits and resonators is not always guaranteed to work
    qubits = [qb for qb in sqa.qubits if qb.startswith('QB')]
    computational_resonators = [qb for qb in sqa.qubits if qb.startswith('COMP')]
    gates = {
        gate_name: GateInfo(
            implementations={'__fake': GateImplementationInfo(loci=tuple(tuple(locus) for locus in gate_loci))},
            default_implementation='__fake',
            override_default_implementation={},
        )
        for gate_name, gate_loci in sqa.operations.items()
    }
    return DynamicQuantumArchitecture(
        calibration_set_id=UUID('00000000-0000-0000-0000-000000000000'),
        qubits=qubits,
        computational_resonators=computational_resonators,
        gates=gates,
    )


def _DQA_to_qiskit_target(
    architecture: DynamicQuantumArchitecture,
) -> tuple[Target, Target, dict[str, int]]:
    """Converts a QuantumArchitectureSpecification object to a Qiskit Target object.

    Args:
        architecture: The quantum architecture specification to convert.

    Returns:
        A Qiskit Target object representing the given quantum architecture specification.
    """
    # pylint: disable=unreachable
    target = Target()

    def get_num_or_zero(name: str) -> int:
        match = re.search(r'(\d+)', name)
        return int(match.group(1)) if match else 0

    component_to_idx = {
        qb: idx
        for idx, qb in enumerate(
            sorted(architecture.computational_resonators + architecture.qubits, key=get_num_or_zero)
        )
    }
    operations = architecture.gates

    # There is no dedicated direct way of setting just the qubit connectivity and the native gates to the target.
    # Such info is automatically deduced once all instruction properties are set. Currently, we do not retrieve
    # any properties from the server, and we are interested only in letting the target know what is the native gate
    # set and the connectivity of the device under use. Thus, we populate the target with None properties.
    def _create_connections(name: str, is_symmetric: bool = False) -> dict[tuple[int, ...], None]:
        """Creates the connection map of allowed loci for this instruction, mapped to None."""
        # if is_multi_qubit_instruction(name):
        #    if is_directed_instruction(name):
        #        return {(qb_to_idx[qb1], qb_to_idx[qb2]): None for [qb1, qb2] in operations[name]}
        #    return {
        #        (qb_to_idx[qb1], qb_to_idx[qb2]): None for pair in operations[name] for qb1, qb2 in (pair, pair[::-1])
        #    }
        gate_info = operations[name]
        all_loci = gate_info.implementations[gate_info.default_implementation].loci
        connections = {tuple(component_to_idx[locus] for locus in loci): None for loci in all_loci}
        if is_symmetric:
            # If the gate is symmetric, we need to add the reverse connections as well.
            connections.update({tuple(reversed(loci)): None for loci in connections})
        return connections

    if 'prx' in operations or 'phased_rx' in operations:
        target.add_instruction(
            RGate(Parameter('theta'), Parameter('phi')),
            _create_connections('prx'),
        )
    if 'cc_prx' in operations:
        # HACK reset gate shares cc_prx loci for now
        target.add_instruction(Reset(), _create_connections('cc_prx'))

    target.add_instruction(
        IGate(), {(component_to_idx[qb],): None for qb in architecture.computational_resonators + architecture.qubits}
    )
    # Even though CZ is a symmetric gate, we still need to add properties for both directions. This is because
    # coupling maps in Qiskit are directed graphs and the gate symmetry is not implicitly planted there. It should
    # be explicitly supplied. This allows Qiskit to have coupling maps with non-symmetric gates like cx.
    if 'measure' in operations:
        target.add_instruction(Measure(), _create_connections('measure'))

    # Special work for devices with a MoveGate.
    fake_target = copy(target)
    if 'cz' in operations:
        target.add_instruction(CZGate(), _create_connections('cz', True))
    if 'move' in operations:
        target.add_instruction(MoveGate(), _create_connections('move'))
        if 'cz' in operations:
            fake_cz_connections: dict[tuple[int, int], None] = {}
            cz_loci = operations['cz'].implementations[operations['cz'].default_implementation].loci
            for qb1, qb2 in cz_loci:
                if (
                    qb1 not in architecture.computational_resonators
                    and qb2 not in architecture.computational_resonators
                ):
                    fake_cz_connections[(component_to_idx[qb1], component_to_idx[qb2])] = None
                    fake_cz_connections[(component_to_idx[qb2], component_to_idx[qb1])] = None
            for qb1, res in operations['move']:
                for qb2 in [q for q in architecture.qubits if q not in [qb1, res]]:
                    if [qb2, res] in cz_loci or [res, qb2] in cz_loci:
                        fake_cz_connections[(component_to_idx[qb1], component_to_idx[qb2])] = None
                        fake_cz_connections[(component_to_idx[qb2], component_to_idx[qb1])] = None
    return target, fake_target, component_to_idx


class IQMBackendBase(BackendV2, ABC):
    """Abstract base class for various IQM-specific backends.

    Args:
        architecture: Description of the quantum architecture associated with the backend instance.
    """

    architecture: DynamicQuantumArchitecture

    def __init__(
        self,
        architecture: Union[QuantumArchitectureSpecification, DynamicQuantumArchitecture],
        **kwargs,
    ):
        super().__init__(**kwargs)
        if isinstance(architecture, QuantumArchitectureSpecification):
            arch = _dqa_from_static_architecture(architecture)
        else:
            arch = architecture
        self.architecture = arch

        # Qiskit uses integer indices to refer to qubits, so we need to map component names to indices.
        qb_to_idx = {qb: idx for idx, qb in enumerate(arch.components)}
        operations = {gate_name: gate_info.loci for gate_name, gate_info in arch.gates.items()}
        target = Target()

        def _create_properties(
            op_name: str, symmetric: bool = False
        ) -> dict[tuple[int, ...], InstructionProperties | None]:
            """Creates the Qiskit instruction properties dictionary for the given IQM native operation.

            Currently we do not provide any actual properties for the operation other than the
            allowed loci.
            """
            loci = operations[op_name]
            if symmetric:
                # For symmetric gates, construct all the valid loci for Qiskit.
                # Coupling maps in Qiskit are directed graphs, and gate symmetry is provided explicitly.
                loci = tuple(permuted_locus for locus in loci for permuted_locus in itertools.permutations(locus))
            return {tuple(qb_to_idx[qb] for qb in locus): None for locus in loci}

        if 'measure' in operations:
            target.add_instruction(Measure(), _create_properties('measure'))
        target.add_instruction(
            IGate(),
            {(qb_to_idx[qb],): None for qb in arch.components},
        )
        if 'prx' in operations:
            target.add_instruction(RGate(Parameter('theta'), Parameter('phi')), _create_properties('prx'))
        if 'cz' in operations:
            target.add_instruction(CZGate(), _create_properties('cz', symmetric=True))
        if 'move' in operations:
            target.add_instruction(MoveGate(), _create_properties('move'))
        if 'cc_prx' in operations:
            # HACK reset gate shares cc_prx loci for now
            target.add_instruction(Reset(), _create_properties('cc_prx'))

        self._physical_target, self._fake_target, self._qb_to_idx = _DQA_to_qiskit_target(arch)
        self._qb_to_idx = qb_to_idx
        self._idx_to_qb = {v: k for k, v in qb_to_idx.items()}
        self.name = 'IQMBackend'

    @property
    def target(self) -> Target:
        return self._physical_target

    @property
    def fake_target(self) -> Target:
        """A target representing the backend where resonators are abstracted away. If the backend does not support
        resonators, this target is the same as the `target` property, but different instances.
        """
        return self._fake_target

    @property
    def physical_target(self) -> Target:
        """A target providing an accurate representation of the backend."""
        return self._physical_target

    @property
    def physical_qubits(self) -> list[str]:
        """Return the list of physical qubits in the backend."""
        return list(self._qb_to_idx)

    def qubit_name_to_index(self, name: str) -> int:
        """Given an IQM-style qubit name, return the corresponding index in the register.

        Args:
            name: IQM-style qubit name ('QB1', 'QB2', etc.)

        Returns:
            Index of the given qubit in the quantum register.

        Raises:
            ValueError if qubit name cannot be found.
        """
        if name not in self._qb_to_idx:
            raise ValueError(f"Qubit name '{name}' is not part of the backend.")
        return self._qb_to_idx[name]

    def index_to_qubit_name(self, index: int) -> str:
        """Given a quantum register index, return the corresponding IQM-style qubit name.

        Args:
            index: Qubit index in the quantum register.

        Returns:
            Corresponding IQM-style qubit name ('QB1', 'QB2', etc.), or ``None`` if
            the given index does not correspond to any qubit on the backend.
        """
        if index not in self._idx_to_qb:
            raise ValueError(f"Qubit index '{index}' is not part of the backend.")
        return self._idx_to_qb[index]

    def get_scheduling_stage_plugin(self) -> str:
        """Return the plugin that should be used for scheduling the circuits on this backend."""
        return 'default'
