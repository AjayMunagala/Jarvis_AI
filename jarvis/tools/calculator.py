"""Safe arithmetic evaluation by AST walking (never ``eval``/``exec``).

Only numeric literals, + - * / // % **, unary +/-, parentheses, and a fixed set
of pure functions are accepted. Names, attributes, calls to anything else,
subscripts, comprehensions, and strings are rejected. Exponents and results are
bounded so a short expression cannot consume unbounded CPU or memory.
"""

import ast
import math
import operator

MAX_EXPRESSION_LENGTH = 200
MAX_NODES = 100
MAX_EXPONENT = 1000
MAX_ABS_RESULT = 1e100

_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {"abs": abs, "min": min, "max": max, "round": round, "sqrt": math.sqrt}


class CalculatorError(ValueError):
    """Rejected or invalid expression; the message is safe to show."""


def _check(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalculatorError("Only numeric results are supported.")
    if isinstance(value, float) and not math.isfinite(value):
        raise CalculatorError("The result is not a finite number.")
    if abs(value) > MAX_ABS_RESULT:
        raise CalculatorError("The result is too large.")
    return value


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return _check(node.value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _check(_UNARY[type(node.op)](_eval(node.operand)))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise CalculatorError("The exponent is too large.")
        if isinstance(node.op, ast.Pow) and abs(left) > 1e6 and right > 50:
            raise CalculatorError("The result is too large.")
        try:
            return _check(_BINARY[type(node.op)](left, right))
        except ZeroDivisionError as exc:
            raise CalculatorError("Division by zero.") from exc
        except OverflowError as exc:
            raise CalculatorError("The result is too large.") from exc
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS
            and not node.keywords and 1 <= len(node.args) <= 5):
        args = [_eval(item) for item in node.args]
        try:
            return _check(_FUNCTIONS[node.func.id](*args))
        except (TypeError, ValueError) as exc:
            raise CalculatorError("Invalid function arguments.") from exc
    raise CalculatorError("Only numbers, arithmetic operators, parentheses, and abs/min/max/round/sqrt are allowed.")


def evaluate(expression: str) -> int | float:
    if not isinstance(expression, str) or not expression.strip():
        raise CalculatorError("An expression is required.")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise CalculatorError("The expression is too long.")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise CalculatorError("The expression is not valid arithmetic.") from exc
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise CalculatorError("The expression is too complex.")
    return _eval(tree)
