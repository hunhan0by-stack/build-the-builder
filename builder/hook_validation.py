"""Narrow static check for retaining a response alias and then clearing it.

This is not a universal Python alias analyzer or native execution verification.
It never executes generated code and does not rewrite a hook.
"""
import ast
from .models import FoundationError


def validate_posthook_retention(code):
    try:
        tree=ast.parse(code)
    except (SyntaxError,ValueError,RecursionError):
        raise FoundationError('BUILDER_POSTHOOK_SYNTAX_INVALID') from None

    def aliases(value,names):
        return (isinstance(value,ast.Name) and value.id in names or
            isinstance(value,ast.IfExp) and (aliases(value.body,names) or aliases(value.orelse,names)))

    def key(target):
        if isinstance(target,ast.Subscript) and isinstance(target.value,ast.Name) and target.value.id=='vars':
            return ast.dump(target.slice,include_attributes=False)
        return None

    def join(states):
        states=[state for state in states if state is not None]
        return (set().union(*(state[0] for state in states)),set().union(*(state[1] for state in states))) if states else None

    def block(statements,state):
        names,saved=map(set,state)
        for statement in statements:
            if isinstance(statement,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                continue
            if isinstance(statement,ast.If):
                merged=join([block(statement.body,(names,saved)),block(statement.orelse,(names,saved))])
                if merged is None:return None
                names,saved=merged
                continue
            if isinstance(statement,ast.Try):
                ordinary=block(statement.body,(names,saved))
                if ordinary is not None:ordinary=block(statement.orelse,ordinary)
                merged=join([ordinary,*[block(handler.body,(names,saved)) for handler in statement.handlers]])
                if merged is None:return None
                names,saved=block(statement.finalbody,merged) or merged
                continue
            if isinstance(statement,(ast.For,ast.While,ast.With)):
                merged=join([(names,saved),block(statement.body,(names,saved))])
                names,saved=merged
                if hasattr(statement,'orelse'):names,saved=block(statement.orelse,(names,saved)) or (names,saved)
                continue
            for item in ast.walk(statement):
                if (saved and isinstance(item,ast.Call) and isinstance(item.func,ast.Attribute)
                        and item.func.attr=='clear' and aliases(item.func.value,names)):
                    raise FoundationError('BUILDER_POSTHOOK_RESPONSE_ALIAS_LOSS')
            if isinstance(statement,(ast.Assign,ast.AnnAssign)):
                targets=statement.targets if isinstance(statement,ast.Assign) else [statement.target]
                is_alias=aliases(statement.value,names)
                for target in targets:
                    if isinstance(target,ast.Name):
                        names.add(target.id) if is_alias else names.discard(target.id)
                    target_key=key(target)
                    if target_key is not None:
                        saved.add(target_key) if is_alias else saved.discard(target_key)
            elif isinstance(statement,ast.Delete):
                for target in statement.targets:
                    saved.discard(key(target))
            elif isinstance(statement,(ast.Return,ast.Raise)):
                return None
        return names,saved

    block(tree.body,({'result'},set()))
