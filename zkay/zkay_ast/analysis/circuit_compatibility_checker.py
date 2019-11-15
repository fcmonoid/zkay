from zkay.type_check.type_exceptions import TypeException
from zkay.zkay_ast.analysis.side_effects import SideEffectsVisitor
from zkay.zkay_ast.ast import ConstructorOrFunctionDefinition, FunctionCallExpr, BuiltinFunction, LocationExpr, \
    Statement, AssignmentStatement, ReturnStatement, ReclassifyExpr, Block, StatementList
from zkay.zkay_ast.visitor.function_visitor import FunctionVisitor


def check_circuit_compliance(ast):
    """
    determines for every function whether it can be used inside a circuit
    """
    v = DirectCanBePrivateDetector()
    v.visit(ast)

    v = IndirectCanBePrivateDetector()
    v.visit(ast)

    v = CircuitComplianceChecker()
    v.visit(ast)


class DirectCanBePrivateDetector(FunctionVisitor):
    def visitBuiltinFunction(self, ast: BuiltinFunction):
        self.visitChildren(ast)

    def visitFunctionCallExpr(self, ast: FunctionCallExpr):
        if isinstance(ast.func, BuiltinFunction):
            can_be_private = ast.func.can_be_private()
            if ast.func.is_eq():
                can_be_private |= ast.annotated_type.type_name.can_be_private()
            elif ast.func.is_ite():
                can_be_private |= ast.annotated_type.type_name.can_be_private()
            ast.statement.function.can_be_private &= can_be_private
        self.visitChildren(ast)

    def visitLocationExpr(self, ast: LocationExpr):
        t = ast.annotated_type.type_name
        ast.statement.function.can_be_private &= (t == t.uint_type() or t == t.bool_type())
        self.visitChildren(ast)

    def visitReclassifyExpr(self, ast: ReclassifyExpr):
        return self.visit(ast.expr)

    def visitAssignmentStatement(self, ast: AssignmentStatement):
        self.visitChildren(ast)

    def visitVariableDeclarationStatement(self, ast: AssignmentStatement):
        self.visitChildren(ast)

    def visitReturnStatement(self, ast: ReturnStatement):
        self.visitChildren(ast)

    def visitStatementList(self, ast: StatementList):
        self.visitChildren(ast)

    def visitStatement(self, ast: Statement):
        # All other statement types are not supported inside circuit (for now)
        ast.function.can_be_private = False


class IndirectCanBePrivateDetector(FunctionVisitor):
    def visitConstructorOrFunctionDefinition(self, ast: ConstructorOrFunctionDefinition):
        if ast.can_be_private:
            for fct in ast.called_functions:
                if not fct.can_be_private:
                    ast.can_be_private = False
                    return


def check_for_side_effects_nonstatic_function_calls_or_not_circuit_inlineable(ast):
    v = SideEffectsVisitor()
    v.visit(ast)

    if v.has_side_effects:
        raise TypeException('Expressions with side effects are not allowed inside private expressions', ast)
    if v.has_nonstatic_fcall:
        raise TypeException('Function calls to non static functions are not allowed inside private expressions', ast)
    if not v.can_be_private:
        raise TypeException('Calls to functions with operations which cannot be expressed as a circuit are not allowed inside private expressions', ast)


class CircuitComplianceChecker(FunctionVisitor):
    def visitReclassifyExpr(self, ast: ReclassifyExpr):
        check_for_side_effects_nonstatic_function_calls_or_not_circuit_inlineable(ast.expr)

    def visitFunctionCallExpr(self, ast: FunctionCallExpr):
        if isinstance(ast.func, BuiltinFunction) and ast.func.is_private:
            for arg in ast.args:
                check_for_side_effects_nonstatic_function_calls_or_not_circuit_inlineable(arg)
        else:
            self.visitChildren(ast)