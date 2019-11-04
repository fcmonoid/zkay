from typing import List

from zkay.compiler.privacy.circuit_generation.circuit_helper import CircuitHelper
from zkay.compiler.privacy.library_contracts import bn128_scalar_field
from zkay.compiler.privacy.proving_schemes.proving_scheme import ProvingScheme, G1Point, G2Point, Proof, VerifyingKey
from zkay.utils.multiline_formatter import MultiLineFormatter


class VerifyingKeyGm17(VerifyingKey):
    def __init__(self, h: G2Point, g_alpha: G1Point, h_beta: G2Point, g_gamma: G1Point, h_gamma: G2Point, query: List[G1Point]):
        self.h = h
        self.g_alpha = g_alpha
        self.h_beta = h_beta
        self.g_gamma = g_gamma
        self.h_gamma = h_gamma
        self.query = query


class ProofGm17(Proof):
    def __init__(self, a: G1Point, b: G2Point, c: G1Point):
        self.a = a
        self.b = b
        self.c = c


class ProvingSchemeGm17(ProvingScheme):
    name = 'gm17'

    def dummy_vk(self) -> VerifyingKeyGm17:
        p1 = G1Point('0', '0')
        p2 = G2Point('0', '0', '0', '0')
        return VerifyingKeyGm17(p2, p1, p2, p1, p2, [p1, p1])

    def generate_verification_contract(self, verification_key: VerifyingKeyGm17, circuit: CircuitHelper, should_hash: bool, primary_inputs: List[str]) -> str:
        vk = verification_key
        inputs = [(e.base_name, e.count) for e in (circuit.in_name_factory, circuit.out_name_factory) if e.count > 0]

        query_length = len(vk.query)
        assert query_length == len(primary_inputs) + 1

        # Verification contract based on (with some modifications by NB):
        # https://github.com/Zokrates/ZoKrates/blob/bb98ab1c0426ceeaa2d181fbfbfdc616b8365c6b/zokrates_core/src/proof_system/bn128/gm17.rs#L199
        assert primary_inputs, "No public inputs"
        first_pi = primary_inputs[0]
        potentially_overflowing_pi = [pi for pi in primary_inputs if pi not in ['1', self.hash_var_name]]

        x = MultiLineFormatter() * f'''\
        pragma solidity ^0.5.0;

        import "{ProvingScheme.verify_libs_contract_filename}";

        contract {circuit.get_circuit_name()} {{''' / f'''\
            using Pairing for *;

            uint256 constant {self.snark_scalar_field_var_name} = {bn128_scalar_field};

            struct VerifyingKey {{
                Pairing.G2Point h;
                Pairing.G1Point g_alpha;
                Pairing.G2Point h_beta;
                Pairing.G1Point g_gamma;
                Pairing.G2Point h_gamma;
                Pairing.G1Point[{query_length}] query;
            }}

            struct Proof {{
                Pairing.G1Point a;
                Pairing.G2Point b;
                Pairing.G1Point c;
            }}

            function verifyingKey() pure internal returns (VerifyingKey memory vk) {{''' / f'''\
                vk.h = Pairing.G2Point({str(vk.h)});
                vk.g_alpha = Pairing.G1Point({str(vk.g_alpha)});
                vk.h_beta = Pairing.G2Point({str(vk.h_beta)});
                vk.g_gamma = Pairing.G1Point({str(vk.g_gamma)});
                vk.h_gamma = Pairing.G2Point({str(vk.h_gamma)});''' * [
                f'vk.query[{idx}] = Pairing.G1Point({str(q)});''' for idx, q in enumerate(vk.query)] * f'''\
                return vk;''' // f'''\
            }}

            function check_verify(uint[8] memory proof_, {', '.join([f'uint[{count}] memory {var}' for var, count in inputs])}) public {{''' / '''\
                Proof memory proof;
                proof.a = Pairing.G1Point(proof_[0], proof_[1]);
                proof.b = Pairing.G2Point([proof_[2], proof_[3]], [proof_[4], proof_[5]]);
                proof.c = Pairing.G1Point(proof_[6], proof_[7]);''' * (
                f'\nuint256 {self.hash_var_name} = uint256(sha256(abi.encodePacked({", ".join([n for n, _ in inputs])}))) % {self.snark_scalar_field_var_name};' if should_hash else '') * ((
                ['// Check that inputs do not overflow'] +
                [f'require({pi} < {self.snark_scalar_field_var_name}, "{pi} outside snark field bounds");\n' for pi in potentially_overflowing_pi]) if potentially_overflowing_pi else '') * \
                'VerifyingKey memory vk = verifyingKey();' * \
                f"Pairing.G1Point memory vk_x = {(f'Pairing.scalar_mul(vk.query[1], {first_pi})' if first_pi != '1' else f'vk.query[1]')};" * [
                f"vk_x = Pairing.addition(vk_x, {(f'Pairing.scalar_mul(vk.query[{idx + 2}], {pi})' if pi != '1' else f'vk.query[{idx + 2}]')});" for idx, pi in enumerate(primary_inputs[1:])] * '''\
                vk_x = Pairing.addition(vk_x, vk.query[0]);

                // Check if proof is correct
                require(Pairing.pairingProd4(vk.g_alpha, vk.h_beta, vk_x, vk.h_gamma, proof.c, vk.h, Pairing.negate(Pairing.addition(proof.a, vk.g_alpha)), Pairing.addition(proof.b, vk.h_beta)));
                require(Pairing.pairingProd2(proof.a, vk.h_gamma, Pairing.negate(vk.g_gamma), proof.b));''' // \
            '}' // \
        '}'

        return x.text
