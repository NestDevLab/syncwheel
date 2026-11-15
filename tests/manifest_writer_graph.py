"""Static call binding used only by the manifest-writer lock guard.

Coverage follows resolved symbols and constructor wiring. Command discovery also
uses a separate conservative graph. An unresolved writer path requires review.
"""

import ast
from collections import defaultdict
from pathlib import Path


WRITER_NAMES = {'save_manifest', 'append_ledger_event'}


class FunctionBody(ast.NodeVisitor):
    def __init__(self, node):
        self.calls = []
        self.loads = []
        self.assignments = []
        self.returns = []
        self.imports = []
        for statement in node.body:
            self.visit(statement)

    def visit_FunctionDef(self, node):
        pass

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Import(self, node):
        self.imports.append(node)

    visit_ImportFrom = visit_Import

    def visit_Call(self, node):
        self.calls.append(node)
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.loads.append(node)

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load):
            self.loads.append(node)
        self.generic_visit(node)

    def visit_Assign(self, node):
        for target in node.targets:
            self.assignments.append((target, node.value, None))
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self.assignments.append((node.target, node.value, node.annotation))
        self.generic_visit(node)

    def visit_Return(self, node):
        if node.value is not None:
            self.returns.append(node.value)
        self.generic_visit(node)


def reachable(graph, roots):
    visited = set()
    pending = list(roots)
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        visited.add(key)
        pending.extend(graph.get(key, set()) - visited)
    return visited


class WriterGraph:
    def __init__(self, scripts_root, writer_sinks):
        self.definitions = {}
        self.classes = set()
        self.aliases = defaultdict(dict)
        self.local_aliases = defaultdict(dict)
        self.bases = {}
        self.by_name = defaultdict(set)
        self.locals = defaultdict(dict)
        self.typed_names = defaultdict(set)
        self.fields = defaultdict(lambda: defaultdict(set))
        self.parameters = defaultdict(lambda: defaultdict(set))
        self.results = defaultdict(set)
        self.factory_owners = defaultdict(set)
        self._load(scripts_root)
        self._bind_values()
        self.proven = {}
        self.wide = {}
        self.unresolved = []
        self.writer_sinks = set(writer_sinks)
        missing_sinks = self.writer_sinks - self.definitions.keys()
        if missing_sinks:
            raise AssertionError(('missing writer definitions', sorted(missing_sinks)))
        self.writers = set(self.writer_sinks)
        for key, (node, body) in self.definitions.items():
            proven = set()
            wide = set()
            for call in body.calls:
                resolved = self.symbols(call.func, key)
                targets = resolved & self.definitions.keys()
                proven.update(targets)
                wide.update(targets)
                leaf = self.leaf(call.func)
                if targets & self.writer_sinks or leaf in WRITER_NAMES:
                    self.writers.add(key)
                if isinstance(call.func, ast.Attribute):
                    receiver = call.func.value
                    direct_self = isinstance(receiver, ast.Name) and receiver.id == 'self'
                    namespace = self.symbols(receiver, key)
                    module_alias = bool(namespace) and all(not name for module, name in namespace)
                    if not direct_self and not module_alias:
                        wide.update(self.by_name.get(leaf, set()))
                if isinstance(call.func, (ast.Name, ast.Attribute)) and not targets:
                    candidates = self.by_name.get(leaf, set())
                    wide.update(candidates)
                    self.unresolved.append((key, call, ast.unparse(call.func), leaf))
            if any(self.symbols(reference, key) & self.writer_sinks
                   for reference in body.loads):
                self.writers.add(key)
            self.proven[key] = proven
            self.wide[key] = wide

    @staticmethod
    def leaf(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    def _load(self, scripts_root):
        for path in sorted(Path(scripts_root).glob('*.py')):
            module = path.stem
            tree = ast.parse(path.read_text(), filename=path.name)
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        name = alias.asname or alias.name.split('.')[0]
                        self.aliases[module][name] = (alias.name, '')
                elif isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        self.aliases[module][alias.asname or alias.name] = (node.module, alias.name)
            self._definitions(module, '', tree.body)

    def _definitions(self, module, parent, statements):
        for node in statements:
            name = getattr(node, 'name', None)
            qualified = f'{parent}.{name}' if parent else name
            if isinstance(node, ast.ClassDef):
                self.classes.add((module, qualified))
                self.bases[(module, qualified)] = node.bases
                self._definitions(module, qualified, node.body)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = (module, qualified)
                self.definitions[key] = (node, FunctionBody(node))
                for child in self.definitions[key][1].imports:
                    if isinstance(child, ast.Import):
                        for alias in child.names:
                            self.local_aliases[key][alias.asname or alias.name.split('.')[0]] = (alias.name, '')
                    elif isinstance(child, ast.ImportFrom) and child.module:
                        for alias in child.names:
                            self.local_aliases[key][alias.asname or alias.name] = (child.module, alias.name)
                self.by_name[name].add(key)
                self._definitions(module, qualified, node.body)

    def global_symbols(self, module, name):
        symbol = self.aliases[module].get(name, (module, name))
        if symbol in self.definitions or symbol in self.classes or not symbol[1]:
            return {symbol}
        return set()

    def annotation(self, expression, key):
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            expression = ast.parse(expression.value, mode='eval').body
        if isinstance(expression, ast.Name):
            return self.global_symbols(key[0], expression.id)
        if isinstance(expression, ast.Attribute):
            result = set()
            for module, prefix in self.annotation(expression.value, key):
                result.add((module, f'{prefix}.{expression.attr}' if prefix else expression.attr))
            return result
        return set()

    def symbols(self, expression, key, wiring=False):
        if isinstance(expression, ast.Name):
            if expression.id == 'self':
                owner = key[1].rpartition('.')[0]
                return {(key[0], owner)} if (key[0], owner) in self.classes else set()
            if expression.id in self.locals[key]:
                if wiring or expression.id in self.typed_names[key]:
                    return self.locals[key][expression.id]
                return set()
            if expression.id in self.local_aliases[key]:
                return {self.local_aliases[key][expression.id]}
            return self.global_symbols(key[0], expression.id)
        if isinstance(expression, ast.Attribute):
            result = set()
            for owner in self.symbols(expression.value, key, wiring):
                result.update(self.fields[owner].get(expression.attr, set()))
                module, prefix = owner
                member = (module, f'{prefix}.{expression.attr}' if prefix else expression.attr)
                if member in self.classes:
                    result.add(member)
                result.update(self.methods(owner, expression.attr))
                if not prefix:
                    result.update(self.global_symbols(module, expression.attr))
            return result
        if isinstance(expression, ast.Call):
            result = set()
            for target in self.symbols(expression.func, key, wiring):
                if target in self.classes:
                    result.add(target)
                else:
                    result.update(self.results[target])
            return result
        if isinstance(expression, ast.IfExp):
            return self.symbols(expression.body, key, wiring) | self.symbols(expression.orelse, key, wiring)
        return set()

    def methods(self, owner, name, seen=None):
        seen = set() if seen is None else seen
        if owner in seen:
            return set()
        seen.add(owner)
        direct = (owner[0], f'{owner[1]}.{name}' if owner[1] else name)
        if direct in self.definitions:
            return {direct}
        for expression in self.bases.get(owner, []):
            for base in self.annotation(expression, owner):
                targets = self.methods(base, name, seen)
                if targets:
                    return targets
        return set()

    def _bind_values(self):
        # Typed locals and parameters are explicit evidence. Untyped arbitrary
        # receivers remain unresolved. Constructor parameters additionally get
        # their concrete owners from actual constructor calls below.
        for key, (node, body) in self.definitions.items():
            arguments = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            for argument in arguments:
                if argument.arg != 'self':
                    self.locals[key][argument.arg] = self.annotation(argument.annotation, key)
                    if argument.annotation is not None:
                        self.typed_names[key].add(argument.arg)
            for target, value, annotation in body.assignments:
                if isinstance(target, ast.Name):
                    self.locals[key].setdefault(target.id, set()).update(self.annotation(annotation, key))
                    if annotation is not None:
                        self.typed_names[key].add(target.id)
        changed = True
        while changed:
            before = self._binding_count()
            for key, (node, body) in self.definitions.items():
                for call in body.calls:
                    owners = self.symbols(call.func, key, True) & self.classes
                    if (isinstance(call.func, ast.Attribute) and call.func.attr == '__init__'
                            and isinstance(call.func.value, ast.Call)
                            and isinstance(call.func.value.func, ast.Name)
                            and call.func.value.func.id == 'super'):
                        owner = (key[0], key[1].rpartition('.')[0])
                        for expression in self.bases.get(owner, []):
                            owners.update(self.annotation(expression, key))
                    for owner in owners:
                        constructor = self.definitions.get((owner[0], owner[1] + '.__init__'))
                        if constructor is None:
                            continue
                        parameters = (constructor[0].args.posonlyargs + constructor[0].args.args)[1:]
                        values = dict(zip((p.arg for p in parameters), call.args))
                        values.update({item.arg: item.value for item in call.keywords if item.arg})
                        for name, value in values.items():
                            self.parameters[owner][name].update(self.symbols(value, key, True))
                for target, value, annotation in body.assignments:
                    values = self.symbols(value, key, True) | self.annotation(annotation, key)
                    if isinstance(target, ast.Name):
                        self.locals[key][target.id].update(values)
                    elif (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                          and target.value.id == 'self'):
                        owner = (key[0], key[1].rpartition('.')[0])
                        if owner in self.classes:
                            if isinstance(value, ast.Name) and key[1].endswith('.__init__'):
                                values = self.parameters[owner].get(value.id, set()) or values
                            self.fields[owner][target.attr].update(values)
                for value in body.returns:
                    self.results[key].update(self.symbols(value, key, True))
            changed = self._binding_count() != before
        for key, owners in self.results.items():
            selected = owners & self.classes
            if len(selected) > 1:
                self.factory_owners[key] = selected

    def _binding_count(self):
        return sum(len(values) for fields in self.fields.values() for values in fields.values()) + sum(
            len(values) for values in self.results.values()
        ) + sum(len(values) for items in self.parameters.values() for values in items.values()) + sum(
            len(values) for items in self.locals.values() for values in items.values()
        )

    def inspect(self, registered, manual_edges=()):
        manual_sites = set()
        for caller, lookup, occurrence, callee in manual_edges:
            if caller not in self.definitions or callee not in self.definitions:
                raise AssertionError(('stale manual edge definition', caller, lookup, callee))
            calls = [call for call in self.definitions[caller][1].calls
                     if ast.unparse(call.func) == lookup]
            if occurrence < 0 or occurrence >= len(calls):
                raise AssertionError(('stale manual edge occurrence', caller, lookup, occurrence))
            call = calls[occurrence]
            if self.leaf(call.func) != callee[1].split('.')[-1]:
                raise AssertionError(('manual edge lookup differs from callee', caller, lookup, callee))
            if callee in self.proven[caller]:
                raise AssertionError(('obsolete resolved manual edge', caller, lookup, callee))
            self.proven[caller].add(callee)
            self.wide[caller].add(callee)
            manual_sites.add((caller, call))
        missing_roots = registered - self.definitions.keys()
        covered = reachable(self.proven, registered)
        reverse = defaultdict(set)
        for caller, callees in self.wide.items():
            for callee in callees:
                reverse[callee].add(caller)
        reaches_writer = reachable(reverse, self.writers)
        unresolved_writers = [
            (caller, call.lineno, lookup) for caller, call, lookup, leaf in self.unresolved
            if (leaf in WRITER_NAMES or self.by_name.get(leaf, set()) & reaches_writer)
            and (caller, call) not in manual_sites
        ]
        derived = {key for key in reaches_writer if key[1].startswith('command_')}
        registered_commands = {key for key in registered if key[1].startswith('command_')}
        return {
            'missing_roots': sorted(missing_roots),
            'uncovered': sorted(self.writers - covered),
            'unregistered_commands': sorted(derived - registered_commands),
            'registered_without_writer': sorted(registered_commands - derived),
            'unresolved_writer_calls': sorted(unresolved_writers),
            'unresolved_ignored': sum(not (leaf in WRITER_NAMES or self.by_name.get(leaf, set()) & reaches_writer)
                                      for caller, call, lookup, leaf in self.unresolved),
            'unresolved_with_manual_edge': sum((caller, call) in manual_sites
                                               for caller, call, lookup, leaf in self.unresolved),
            'proven_nodes': len(self.definitions),
            'proven_edges': sum(map(len, self.proven.values())),
            'wide_edges': sum(map(len, self.wide.values())),
            'covered': len(covered),
            'wide_covered': len(reachable(self.wide, registered)),
            'roots': sorted(registered),
            'manual_edges': len(manual_edges),
            'writer_sinks': sorted(self.writer_sinks),
            'multi_owner_factories': {'.'.join(key): sorted(values) for key, values in self.factory_owners.items()},
        }
