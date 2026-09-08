"""Where the prose is in a Python file.

Shared by :mod:`check_comment_drift` and :mod:`changed_comments`, which both
need the same answer and must not answer it differently: one refuses a line,
the other decides whether that line is even in scope for review.

The distinction the raw text cannot make is the one that matters.  ``# Sprint
92`` inside a string literal is data, not a comment, and a checker that reads
diff lines as text refuses it anyway.  ``tokenize`` knows which ``#`` opens a
comment and ``ast`` knows which triple-quoted string is a docstring, so both
questions are answered by the grammar rather than by a pattern.
"""
from __future__ import annotations

import ast
import io
import tokenize


def comment_lines(src: str) -> set[int]:
    """Line numbers carrying a ``#`` comment, per the tokenizer."""
    lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                lines.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # A file the tokenizer cannot finish has no comment positions to
        # report.  Callers treat the empty set as "nothing located here",
        # which understates rather than invents.
        pass
    return lines


def docstring_lines(src: str) -> set[int]:
    """Line numbers inside a module, class or function docstring."""
    lines: set[int] = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return lines
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, 'body', None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            lines.update(range(first.lineno,
                               (first.end_lineno or first.lineno) + 1))
    return lines


def prose_lines(src: str) -> set[int]:
    """Every line of *src* that is comment or docstring."""
    return comment_lines(src) | docstring_lines(src)
