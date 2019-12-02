import os
from hashlib import sha512
from typing import List, Optional

import zkay.config as cfg
import zkay.jsnark_interface.jsnark_interface as jsnark
import zkay.jsnark_interface.libsnark_interface as libsnark
from zkay.compiler.privacy.circuit_generation.circuit_constraints import CircAssignment, CircComment, CircIndentBlock, \
    CircGuardModification, CircCall
from zkay.compiler.privacy.circuit_generation.circuit_generator import CircuitGenerator
from zkay.compiler.privacy.circuit_generation.circuit_helper import CircuitHelper, CircuitStatement, \
    CircVarDecl, CircEqConstraint, CircEncConstraint, HybridArgumentIdf
from zkay.compiler.privacy.proving_schemes.gm17 import ProvingSchemeGm17, VerifyingKeyGm17
from zkay.compiler.privacy.proving_schemes.proving_scheme import VerifyingKey, G2Point, G1Point, ProvingScheme
from zkay.zkay_ast.ast import FunctionCallExpr, BuiltinFunction, IdentifierExpr, BooleanLiteralExpr, \
    IndexExpr, NumberLiteralExpr, MemberAccessExpr, AST, TypeName, indent
from zkay.zkay_ast.visitor.visitor import AstVisitor


class JsnarkVisitor(AstVisitor):
    def __init__(self, phi: List[CircuitStatement]):
        super().__init__('node-or-children', False)
        self.phi = phi

    def visitCircuit(self) -> List[str]:
        return [self.visit(constr) for constr in self.phi]

    def visitCircComment(self, stmt: CircComment):
        return f'// {stmt.text}' if stmt.text else ''

    def visitCircIndentBlock(self, stmt: CircIndentBlock):
        stmts = list(map(self.visit, stmt.statements))
        return f'/*** BEGIN {stmt.name} ***/\n' + indent('\n'.join(stmts)) + '\n' + f'/***  END  {stmt.name} ***/'

    def visitCircCall(self, stmt: CircCall):
        return f'_{stmt.fct.unambiguous_name}();'

    def visitCircVarDecl(self, stmt: CircVarDecl):
        assert stmt.lhs.t.size_in_uints == 1
        return f'assign("{stmt.lhs.name}", {self.visit(stmt.expr)});'

    def visitCircEqConstraint(self, stmt: CircEqConstraint):
        assert stmt.tgt.t.size_in_uints == stmt.val.t.size_in_uints
        return f'checkEq("{stmt.tgt.name}", "{stmt.val.name}");'

    def visitCircEncConstraint(self, stmt: CircEncConstraint):
        assert stmt.cipher.t == TypeName.cipher_type()
        assert stmt.pk.t == TypeName.key_type()
        assert stmt.rnd.t == TypeName.rnd_type()
        if stmt.is_dec:
            return f'checkDec("{stmt.plain.name}", "{stmt.pk.name}", "{stmt.rnd.name}", "{stmt.cipher.name}");'
        else:
            return f'checkEnc("{stmt.plain.name}", "{stmt.pk.name}", "{stmt.rnd.name}", "{stmt.cipher.name}");'

    def visitCircAssignment(self, stmt: CircAssignment):
        assert isinstance(stmt.lhs, IdentifierExpr)
        return f'assign("{stmt.lhs.names}", {self.visit(stmt.rhs)});'

    def visitCircGuardModification(self, stmt: CircGuardModification):
        if stmt.new_cond is None:
            return 'popGuard();'
        else:
            return f'addGuard("{stmt.new_cond.name}", {str(stmt.is_true).lower()});'

    def visitBooleanLiteralExpr(self, ast: BooleanLiteralExpr):
        return f'val({str(ast.value).lower()})'

    def visitNumberLiteralExpr(self, ast: NumberLiteralExpr):
        if ast.value < (1 << 31):
            return f'val({ast.value})'
        else:
            return f'val("{ast.value}")'

    def visitIdentifierExpr(self, ast: IdentifierExpr):
        return f'get("{ast.idf.name}")'

    def visitMemberAccessExpr(self, ast: MemberAccessExpr):
        assert isinstance(ast.member, HybridArgumentIdf) and ast.member.t.size_in_uints == 1
        return f'get("{ast.member.name}")'

    def visitIndexExpr(self, ast: IndexExpr):
        raise NotImplementedError()

    def visitFunctionCallExpr(self, ast: FunctionCallExpr):
        if isinstance(ast.func, BuiltinFunction):
            op = ast.func.op
            args = list(map(self.visit, ast.args))

            if op == 'ite':
                fstr = 'ite({}, {}, {})[0]'
            elif op == 'parenthesis':
                fstr = '({})'

            elif op == 'sign+':
                fstr = '{}'
            elif op == 'sign-':
                fstr = '{}.mul(-1)'

            elif op == '*':
                fstr = '{}.mul({})'
            elif op == '+':
                fstr = '{}.add({})'
            elif op == '-':
                fstr = '{}.sub({})'

            elif op == '==':
                fstr = '{}.isEqualTo({})'
            elif op == '!=':
                fstr = '{}.sub({}).checkNonZero()'

            elif op == '<':
                fstr = '{}.isLessThan({}, 253)'
            elif op == '<=':
                fstr = '{}.isLessThanOrEqual({}, 253)'
            elif op == '>':
                fstr = '{}.isGreaterThan({}, 253)'
            elif op == '>=':
                fstr = '{}.isGreaterThanOrEqual({}, 253)'

            elif op == '&&':
                fstr = '{}.and({})'
            elif op == '||':
                fstr = '{}.or({})'
            elif op == '!':
                fstr = '{}.invAsBits()'
            else:
                raise ValueError(f'Unsupported builtin function {ast.func.op}')

            return fstr.format(*args)

        raise ValueError(f'Unsupported function {ast.func.code()} inside circuit')


def add_function_circuit_arguments(circuit: CircuitHelper):
    input_init_stmts = []
    for sec_input in circuit.sec_idfs:
        input_init_stmts.append(f'addS("{sec_input.name}", {sec_input.t.size_in_uints});')

    for pub_input in circuit.input_idfs:
        addf = 'addK' if pub_input.t == TypeName.key_type() else 'addIn'
        input_init_stmts.append(f'{addf}("{pub_input.name}", {pub_input.t.size_in_uints});')

    for pub_output in circuit.output_idfs:
        input_init_stmts.append(f'addOut("{pub_output.name}", {pub_output.t.size_in_uints});')

    return input_init_stmts


class JsnarkGenerator(CircuitGenerator):
    def __init__(self, transformed_ast: AST, circuits: List[CircuitHelper], proving_scheme: ProvingScheme, output_dir: str):
        super().__init__(transformed_ast, circuits, proving_scheme, output_dir, False)

    def _generate_zkcircuit(self, circuit: CircuitHelper) -> bool:
        output_dir = self._get_circuit_output_dir(circuit)
        if not os.path.exists(output_dir):
            os.mkdir(output_dir)

        fdefs = []
        for fct in list(circuit.fct.called_functions.keys()):
            if fct.requires_verification:
                target_circuit = self.circuits[fct]
                body_stmts = JsnarkVisitor(target_circuit.phi).visitCircuit()

                body = '\n'.join([f'stepIn("{fct.unambiguous_name}");'] +
                                 add_function_circuit_arguments(target_circuit) + [''] +
                                 [stmt.strip() for stmt in body_stmts] +
                                 ['stepOut();'])
                fdef = f'private void _{fct.unambiguous_name}() {{\n' + indent(body) + '\n}'
                fdefs.append(f'{fdef}')

        input_init_stmts = add_function_circuit_arguments(circuit)
        constraints = JsnarkVisitor(circuit.phi).visitCircuit()

        code = jsnark.get_jsnark_circuit_class_str(circuit.get_circuit_name(),
                                                   circuit.in_size_trans, circuit.out_size_trans, circuit.priv_in_size_trans,
                                                   fdefs, input_init_stmts, constraints)

        hashfile = os.path.join(output_dir, f'{cfg.jsnark_circuit_classname}.sha512')
        hash = sha512((jsnark.circuit_builder_jar_hash + code).encode('utf-8')).hexdigest()
        if os.path.exists(hashfile):
            with open(hashfile, 'r') as f:
                oldhash = f.read()
        else:
            oldhash = ''

        if oldhash != hash or not os.path.exists(os.path.join(output_dir, 'circuit.arith')):
            # Remove old keys
            for f in self._get_vk_and_pk_paths(circuit):
                if os.path.exists(f):
                    os.remove(f)
            jsnark.compile_circuit(output_dir, code)
            with open(hashfile, 'w') as f:
                f.write(hash)
            return True
        else:
            print(f'Circuit \'{circuit.get_circuit_name()}\' not modified, skipping compilation')
            return False

    def _generate_keys(self, circuit: CircuitHelper):
        output_dir = self._get_circuit_output_dir(circuit)
        libsnark.generate_keys(output_dir, self.proving_scheme.name)

    def _get_vk_and_pk_paths(self, circuit: CircuitHelper):
        output_dir = self._get_circuit_output_dir(circuit)
        return os.path.join(output_dir, 'verification.key'), os.path.join(output_dir, 'proving.key')

    def _parse_verification_key(self, circuit: CircuitHelper) -> VerifyingKey:
        with open(self._get_vk_and_pk_paths(circuit)[0]) as f:
            data = iter(f.read().splitlines())
        if isinstance(self.proving_scheme, ProvingSchemeGm17):
            h = G2Point(next(data), next(data), next(data), next(data))
            g_alpha = G1Point(next(data), next(data))
            h_beta = G2Point(next(data), next(data), next(data), next(data))
            g_gamma = G1Point(next(data), next(data))
            h_gamma = G2Point(next(data), next(data), next(data), next(data))
            query_len = int(next(data))
            query: List[Optional[G1Point]] = [None for _ in range(query_len)]
            for idx in range(query_len):
                query[idx] = G1Point(next(data), next(data))
            return VerifyingKeyGm17(h, g_alpha, h_beta, g_gamma, h_gamma, query)
        else:
            raise NotImplementedError()

    def _get_primary_inputs(self, should_hash: bool, circuit: CircuitHelper) -> List[str]:
        return ['1'] + super()._get_primary_inputs(should_hash, circuit)
