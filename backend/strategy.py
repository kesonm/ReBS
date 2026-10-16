"""Strategy definitions, bounded sample expressions and persistent draft configuration.

This module never submits orders or evaluates Python code. Factor formulas in the
catalog are definitions; preview evaluates only explicitly supplied sample values.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import keyword
import math
import operator
import os
from pathlib import Path
import re
import sqlite3
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .demo import require_private_access
from .okx import instrument_id, period_id
from .service import InputError, PROJECT_ROOT

CATALOG_PATH = Path(__file__).with_name('strategy_catalog.json')
MAX_BODY = 262144
MAX_EXPRESSION = 1600
MAX_SERIES = 2048
MAX_NODES = 256
MAX_WORK = 500000
NO_STORE = {'Cache-Control': 'no-store'}
FUNCTION_ARITY = {
    'abs': (1, 1), 'min': (2, 10), 'max': (2, 10), 'round': (1, 2),
    'sqrt': (1, 1), 'log': (1, 2), 'exp': (1, 1), 'clamp': (3, 3),
    'lag': (1, 2), 'sma': (2, 2), 'ema': (2, 2), 'std': (2, 2),
    'sum': (2, 2), 'highest': (2, 2), 'lowest': (2, 2),
    'cross_above': (2, 2), 'cross_below': (2, 2), 'pct_change': (1, 2),
}
ALIAS = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,47}$')
RESERVED = set(FUNCTION_ARITY) | set(keyword.kwlist)


def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding='utf-8'))


def issue(path: str, message: str) -> dict:
    return {'path': path, 'message': message}


class ExpressionError(ValueError):
    pass


def finite(value):
    if isinstance(value, bool):
        return value
    try:
        valid = isinstance(value, (int, float)) and math.isfinite(value) and abs(value) <= 1e100
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ExpressionError('数值必须有限且绝对值不超过 1e100。')
    return value


def parse_expression(expression: str, names: set[str]) -> tuple[ast.Expression, set[str]]:
    if not isinstance(expression, str) or not expression.strip() or len(expression) > MAX_EXPRESSION:
        raise ExpressionError(f'公式不能为空，且最多 {MAX_EXPRESSION} 个字符。')
    try:
        tree = ast.parse(expression.strip(), mode='eval')
    except (SyntaxError, ValueError, UnicodeError, RecursionError) as exc:
        raise ExpressionError('公式语法错误；使用 and、or、not 和 True / False。') from exc
    allowed = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
               ast.UnaryOp, ast.UAdd, ast.USub, ast.Not, ast.BoolOp, ast.And,
               ast.Or, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
               ast.Gt, ast.GtE, ast.Call)
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise ExpressionError(f'公式最多包含 {MAX_NODES} 个语法节点。')
    references = set()
    function_nodes = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
    stack = [(tree, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > 32:
            raise ExpressionError('公式嵌套最多 32 层。')
        if not isinstance(node, allowed):
            raise ExpressionError('仅允许数值、布尔逻辑、算术比较及列出的函数；禁止属性访问、下标和 Python 代码。')
        if isinstance(node, ast.Constant):
            finite(node.value)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTION_ARITY:
                raise ExpressionError('只能调用函数目录中列出的函数。')
            lo, hi = FUNCTION_ARITY[node.func.id]
            if node.keywords or not lo <= len(node.args) <= hi:
                raise ExpressionError(f'{node.func.id} 的参数数量应为 {lo}–{hi}，且不支持关键字参数。')
            if node.func.id in {'lag', 'pct_change', 'sma', 'ema', 'std', 'sum', 'highest', 'lowest'} and len(node.args) == 2:
                window = node.args[1]
                if isinstance(window, ast.UnaryOp) and isinstance(window.op, ast.USub):
                    raise ExpressionError('历史窗口必须为正整数，禁止读取未来数据。')
                if isinstance(window, ast.Constant) and (type(window.value) is not int or window.value < 1):
                    raise ExpressionError('历史窗口必须为正整数，禁止读取未来数据。')
        elif isinstance(node, ast.Name):
            if node.id in FUNCTION_ARITY and id(node) not in function_nodes:
                raise ExpressionError(f'{node.id} 是函数，必须使用函数调用语法。')
            if node.id not in names and node.id not in FUNCTION_ARITY:
                raise ExpressionError(f'未知变量：{node.id}。请先添加因子或全局参数。')
            if node.id in names:
                references.add(node.id)
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return tree, references


def normalize_parameters(raw, definitions: list[dict], path: str, errors: list) -> dict:
    if not isinstance(raw, dict):
        errors.append(issue(path, '参数必须为对象。'))
        raw = {}
    defined = {p['id']: p for p in definitions}
    for key in raw:
        if key not in defined:
            errors.append(issue(f'{path}.{key}', '未知参数；不能静默忽略。'))
    normalized = {}
    for key, definition in defined.items():
        value = raw.get(key, definition.get('default'))
        kind = definition['type']
        valid = True
        if kind in {'number', 'integer'}:
            try:
                valid = type(value) in (int, float) and finite(value) == value
            except ExpressionError:
                valid = False
            if kind == 'integer':
                valid = valid and type(value) is int
            if valid:
                valid = ('min' not in definition or value >= definition['min']) and ('max' not in definition or value <= definition['max'])
        elif kind == 'boolean':
            valid = type(value) is bool
        elif kind == 'select':
            valid = any(type(value) is type(option['value']) and value == option['value'] for option in definition.get('options', []))
        elif kind == 'string':
            valid = isinstance(value, str) and len(value) <= 500
        else:
            valid = False
        if not valid:
            errors.append(issue(f'{path}.{key}', f'参数类型、范围或选项不正确：{definition.get("label", key)}。'))
        else:
            normalized[key] = value
    return normalized


def validate_relations(parameters: dict, path: str, errors: list, *, global_parameters=False):
    """Validate only relationships whose meanings are explicit in the catalog."""
    pairs = [('fast', 'slow', True), ('ddof', 'n', True)]
    if global_parameters:
        pairs = [('fast_period', 'slow_period', True), ('rsi_lower', 'rsi_upper', True),
                 ('min_order_notional', 'max_order_notional', False),
                 ('min_order_quantity', 'max_order_quantity', False),
                 ('min_volatility', 'max_volatility', False),
                 ('retry_backoff_ms', 'retry_backoff_max_ms', False),
                 ('reconnect_backoff_seconds', 'reconnect_max_backoff_seconds', False),
                 ('warmup_bars', 'history_bars', False)]
    for lower, upper, strict in pairs:
        if lower in parameters and upper in parameters:
            if not (parameters[lower] < parameters[upper] if strict else parameters[lower] <= parameters[upper]):
                errors.append(issue(f'{path}.{lower}', f'{lower} 必须{"小于" if strict else "小于或等于"} {upper}。'))
    for key in ['benchmark', 'benchmark_instrument']:
        if key in parameters:
            try:
                instrument_id(parameters[key])
            except (InputError, TypeError, AttributeError):
                errors.append(issue(f'{path}.{key}', '基准币对格式不正确，例如 BTC-USDT。'))
    if parameters.get('anchor_time'):
        try:
            anchor = datetime.fromisoformat(parameters['anchor_time'].replace('Z', '+00:00'))
            if anchor.utcoffset() is None:
                raise ValueError('timezone required')
        except (ValueError, TypeError, AttributeError):
            errors.append(issue(f'{path}.anchor_time', '锚定时间必须为带时区的 ISO 8601 时间。'))
    if not global_parameters:
        return
    for key in ['trading_session_start', 'trading_session_end']:
        if key in parameters and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', str(parameters[key])):
            errors.append(issue(f'{path}.{key}', '交易时段必须使用 00:00–23:59 的 HH:mm 格式；允许跨午夜。'))
    if parameters.get('sizing_mode') == 'risk_fraction' and not parameters.get('stop_loss_enabled'):
        errors.append(issue(f'{path}.sizing_mode', '按止损风险预算计算仓位时，必须启用并配置初始止损。'))
    if parameters.get('take_profit_enabled') and parameters.get('take_profit_mode') == 'risk_reward' and not parameters.get('stop_loss_enabled'):
        errors.append(issue(f'{path}.take_profit_mode', '盈亏比止盈需要启用初始止损。'))
    needs_stop_distance = parameters.get('sizing_mode') == 'risk_fraction' or (parameters.get('take_profit_enabled') and parameters.get('take_profit_mode') == 'risk_reward')
    stop_field = {'percent': 'stop_loss_fraction', 'atr': 'stop_loss_atr_multiple', 'quote_amount': 'stop_loss_quote_amount'}.get(parameters.get('stop_loss_mode'))
    if needs_stop_distance and stop_field in parameters and parameters[stop_field] <= 0:
        errors.append(issue(f'{path}.{stop_field}', '风险预算或盈亏比计算要求初始止损距离大于 0。'))
    if 'train_fraction' in parameters and 'validation_fraction' in parameters:
        if parameters['train_fraction'] + parameters['validation_fraction'] >= 1:
            errors.append(issue(f'{path}.validation_fraction', '训练集和验证集占比之和必须小于 1，需保留测试集。'))
    dates = {}
    for key in ['backtest_start', 'backtest_end']:
        if parameters.get(key):
            try:
                dates[key] = datetime.fromisoformat(parameters[key].replace('Z', '+00:00'))
                if dates[key].utcoffset() is None:
                    errors.append(issue(f'{path}.{key}', '回测日期必须明确时区，例如 2026-01-01T00:00:00+08:00。'))
            except (ValueError, TypeError, AttributeError):
                errors.append(issue(f'{path}.{key}', '回测日期必须为带时区的 ISO 8601，例如 2026-01-01T00:00:00+08:00。'))
    if len(dates) == 2:
        try:
            if dates['backtest_start'] >= dates['backtest_end']:
                errors.append(issue(f'{path}.backtest_end', '回测结束时间必须晚于开始时间。'))
        except TypeError:
            errors.append(issue(f'{path}.backtest_end', '回测起止时间必须使用一致的时区格式。'))


def validate_draft(value, definitions: dict | None = None) -> dict:
    definitions = definitions or catalog()
    errors, warnings, dependencies = [], [], {}
    if not isinstance(value, dict):
        return {'valid': False, 'errors': [issue('draft', '策略必须为 JSON 对象。')], 'warnings': [], 'dependencies': [], 'normalized': None}
    fields = {'id', 'name', 'description', 'instrument', 'period', 'parameters', 'factors', 'customFactors', 'rules', 'revision', 'createdAt', 'updatedAt'}
    for key in value.keys() - fields:
        errors.append(issue(key, '未知策略字段。'))
    normalized = {}
    for key, default, maximum in [('name', '', 100), ('description', '', 3000)]:
        item = value.get(key, default)
        if not isinstance(item, str) or len(item) > maximum or (key == 'name' and not item.strip()):
            errors.append(issue(key, f'请输入有效文字（最多 {maximum} 字）。'))
        else:
            normalized[key] = item.strip()
    for key, normalizer, default in [('instrument', instrument_id, 'BTC-USDT'), ('period', period_id, '15m')]:
        try:
            item = value.get(key, default)
            if not isinstance(item, str):
                raise InputError('应为字符串。')
            normalized[key] = normalizer(item)
        except InputError as exc:
            errors.append(issue(key, str(exc)))
    if 'id' in value:
        try:
            normalized['id'] = str(UUID(value['id']))
        except (ValueError, TypeError, AttributeError):
            errors.append(issue('id', '策略 ID 必须为 UUID。'))
    if 'revision' in value:
        if type(value['revision']) is not int or not 1 <= value['revision'] <= 1000000000:
            errors.append(issue('revision', '版本号必须为正整数。'))
        else:
            normalized['revision'] = value['revision']
    normalized['parameters'] = normalize_parameters(value.get('parameters', {}), definitions['parameters'], 'parameters', errors)
    validate_relations(normalized['parameters'], 'parameters', errors, global_parameters=True)
    names = set(p['id'] for p in definitions['parameters'])
    known = {factor['id']: factor for factor in definitions['factors']}
    selected = value.get('factors', [])
    customs = value.get('customFactors', [])
    if not isinstance(selected, list) or len(selected) > 100:
        errors.append(issue('factors', '因子实例必须为数组，最多 100 个。'))
        selected = []
    if not isinstance(customs, list) or len(customs) > 30:
        errors.append(issue('customFactors', '自定义因子必须为数组，最多 30 个。'))
        customs = []
    normalized['factors'], normalized['customFactors'] = [], []
    for section, instances in [('factors', selected), ('customFactors', customs)]:
        for index, instance in enumerate(instances):
            path = f'{section}.{index}'
            if not isinstance(instance, dict):
                errors.append(issue(path, '因子实例必须为对象。'))
                continue
            allowed = {'alias', 'factorId', 'params'} if section == 'factors' else {'alias', 'name', 'expression', 'unit', 'description'}
            for key in instance.keys() - allowed:
                errors.append(issue(f'{path}.{key}', '未知因子配置字段。'))
            alias = instance.get('alias')
            if not isinstance(alias, str) or not ALIAS.fullmatch(alias) or alias in names or alias in RESERVED:
                errors.append(issue(f'{path}.alias', '变量名以英文字母开头，仅限字母数字下划线，且不能重复或占用参数、函数名称。'))
                continue
            names.add(alias)
            if section == 'factors':
                factor = known.get(instance.get('factorId')) if isinstance(instance.get('factorId'), str) else None
                if factor is None:
                    errors.append(issue(f'{path}.factorId', '因子不在当前目录中。'))
                    continue
                params = normalize_parameters(instance.get('params', {}), factor.get('parameters', []), f'{path}.params', errors)
                validate_relations(params, f'{path}.params', errors)
                normalized[section].append({'alias': alias, 'factorId': factor['id'], 'params': params})
                for source in factor.get('dataSources', []):
                    status = factor.get('dataStatus', 'not_connected')
                    priority = {'available': 0, 'requires_history': 1, 'not_connected': 2}
                    if priority.get(status, 2) >= priority.get(dependencies.get(source, 'available'), 0):
                        dependencies[source] = status
                if factor.get('dataStatus') != 'available':
                    warnings.append(issue(path, '该因子需要补齐历史数据或尚未接入的数据源；当前仅保存定义。'))
            else:
                item = {'alias': alias}
                for key, maximum in [('name', 100), ('expression', MAX_EXPRESSION), ('unit', 100), ('description', 1500)]:
                    text = instance.get(key, alias if key == 'name' else '')
                    if not isinstance(text, str) or len(text) > maximum:
                        errors.append(issue(f'{path}.{key}', f'必须为不超过 {maximum} 字的文本。'))
                    else:
                        item[key] = text
                normalized[section].append(item)
    formula_names = names - {p['id'] for p in definitions['parameters'] if p['type'] not in {'number', 'integer', 'boolean'}}
    graph = {}
    for index, custom in enumerate(normalized['customFactors']):
        try:
            _, refs = parse_expression(custom.get('expression', ''), formula_names)
            graph[custom['alias']] = refs
        except ExpressionError as exc:
            errors.append(issue(f'customFactors.{index}.expression', str(exc)))
    visiting, visited = set(), set()

    def visit(alias):
        if alias in visiting:
            raise ExpressionError(f'自定义因子存在循环依赖：{alias}。')
        if alias in visited or alias not in graph:
            return
        visiting.add(alias)
        for ref in graph[alias]:
            visit(ref)
        visiting.remove(alias)
        visited.add(alias)

    try:
        for alias in graph:
            visit(alias)
    except ExpressionError as exc:
        errors.append(issue('customFactors', str(exc)))
    raw_rules = value.get('rules', {})
    if not isinstance(raw_rules, dict):
        errors.append(issue('rules', '规则必须为对象。'))
        raw_rules = {}
    for key in raw_rules.keys() - {'entry', 'exit', 'filter'}:
        errors.append(issue(f'rules.{key}', '未知规则字段。'))
    normalized['rules'] = {}
    for key, default in [('entry', 'False'), ('exit', 'False'), ('filter', 'True')]:
        expression = raw_rules.get(key, default)
        try:
            parse_expression(expression, formula_names)
            normalized['rules'][key] = expression.strip()
        except ExpressionError as exc:
            errors.append(issue(f'rules.{key}', str(exc)))
    warnings.append(issue('runtime', '本版本为配置与样本公式检查；目录因子尚未自动计算，保存策略不会启动回测或下单。'))
    return {'valid': not errors, 'errors': errors, 'warnings': warnings,
            'dependencies': [{'source': key, 'status': status} for key, status in sorted(dependencies.items())],
            'normalized': normalized if not errors else None}


class SampleEvaluator:
    """Interpret a small AST with bounded arithmetic; no eval, exec or attributes."""
    def __init__(self, values: dict, custom: dict[str, ast.Expression]):
        self.values = values
        self.custom = custom
        self.work = 0
        self.active = set()

    def charge(self, amount=1):
        self.work += amount
        if self.work > MAX_WORK:
            raise ExpressionError('样本计算超过工作量限制；请缩短序列或公式。')

    def apply(self, function, *args):
        lengths = {len(x) for x in args if isinstance(x, list)}
        if len(lengths) > 1:
            raise ExpressionError('参加同一公式的序列必须等长，并按相同时间由旧到新对齐。')
        if lengths:
            length = lengths.pop()
            self.charge(length)
            return [self.scalar(function, *(x[i] if isinstance(x, list) else x for x in args)) for i in range(length)]
        self.charge()
        return self.scalar(function, *args)

    @staticmethod
    def scalar(function, *args):
        if any(value is None for value in args):
            return None
        try:
            return finite(function(*args))
        except ExpressionError:
            raise
        except (ZeroDivisionError, OverflowError, ValueError, TypeError) as exc:
            raise ExpressionError(f'公式无法计算：{str(exc)[:100]}') from exc

    @staticmethod
    def logical(operation, *values):
        if any(type(value) is not bool for value in values):
            raise ExpressionError('and、or、not 的操作数必须为布尔值；请先进行数值比较。')
        if operation == 'not':
            return not values[0]
        return all(values) if operation == 'and' else any(values)

    def name(self, name):
        if name in self.values:
            return self.values[name]
        if name in self.custom:
            if name in self.active:
                raise ExpressionError(f'循环依赖：{name}。')
            self.active.add(name)
            try:
                self.values[name] = self.evaluate(self.custom[name])
            finally:
                self.active.remove(name)
            return self.values[name]
        raise ExpressionError(f'缺少样本值：{name}；系统不会用 0 替代。')

    def evaluate(self, node):
        self.charge()
        if isinstance(node, ast.Expression):
            return self.evaluate(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self.name(node.id)
        if isinstance(node, ast.UnaryOp):
            function = {ast.UAdd: operator.pos, ast.USub: operator.neg,
                        ast.Not: lambda value: self.logical('not', value)}[type(node.op)]
            return self.apply(function, self.evaluate(node.operand))
        if isinstance(node, ast.BinOp):
            function = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                        ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: self.power}[type(node.op)]
            return self.apply(function, self.evaluate(node.left), self.evaluate(node.right))
        if isinstance(node, ast.BoolOp):
            values = [self.evaluate(value) for value in node.values]
            operation = 'and' if isinstance(node.op, ast.And) else 'or'
            function = lambda *args: self.logical(operation, *args)
            return self.apply(function, *values)
        if isinstance(node, ast.Compare):
            values = [self.evaluate(node.left)] + [self.evaluate(value) for value in node.comparators]
            functions = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
                         ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}
            comparisons = [self.apply(functions[type(op)], values[i], values[i + 1]) for i, op in enumerate(node.ops)]
            return self.apply(lambda *args: all(args), *comparisons)
        if isinstance(node, ast.Call):
            return self.call(node.func.id, [self.evaluate(arg) for arg in node.args])
        raise ExpressionError('不支持的表达式。')

    @staticmethod
    def power(left, right):
        if abs(right) > 16:
            raise ExpressionError('幂指数的绝对值最多 16。')
        return left ** right

    def call(self, name, args):
        def clamp(value, lower, upper):
            if lower > upper:
                raise ExpressionError('clamp 的下界不能大于上界。')
            return min(max(value, lower), upper)

        functions = {'abs': abs, 'min': min, 'max': max, 'sqrt': math.sqrt,
                     'log': math.log, 'exp': math.exp,
                     'clamp': clamp}
        if name in functions:
            if name == 'exp' and any((x > 230) for x in (args[0] if isinstance(args[0], list) else [args[0]]) if x is not None):
                raise ExpressionError('exp 输入过大。')
            return self.apply(functions[name], *args)
        if name == 'round':
            digits = args[1] if len(args) == 2 else 0
            if type(digits) is not int or not -15 <= digits <= 15:
                raise ExpressionError('round 精度必须是 −15 到 15 的整数。')
            return self.apply(lambda value: round(value, digits), args[0])
        if name in {'cross_above', 'cross_below'}:
            lengths = {len(arg) for arg in args if isinstance(arg, list)}
            if len(lengths) != 1 or next(iter(lengths)) < 2:
                raise ExpressionError('交叉判断需要至少两个时间点、等长的历史序列。')
            length = lengths.pop()
            left, right = [arg if isinstance(arg, list) else [arg] * length for arg in args]
            self.charge(length)
            result = [None]
            for i in range(1, length):
                points = left[i-1], right[i-1], left[i], right[i]
                if any(value is None for value in points):
                    result.append(None)
                else:
                    result.append((points[0] <= points[1] and points[2] > points[3]) if name == 'cross_above'
                                  else (points[0] >= points[1] and points[2] < points[3]))
            return result
        series = args[0]
        window = args[1] if len(args) == 2 else 1
        if type(window) is not int or not 1 <= window <= 10000:
            raise ExpressionError('窗口必须为 1–10000 的整数；禁止零窗口或未来引用。')
        if not isinstance(series, list):
            raise ExpressionError(f'{name} 需要按时间由旧到新的样本序列。')
        if name in {'lag', 'pct_change'}:
            if len(series) <= window:
                raise ExpressionError(f'{name} 至少需要 {window + 1} 个样本。')
            previous = [None] * window + series[:-window]
            return previous if name == 'lag' else self.apply(lambda current, prior: current / prior - 1, series, previous)
        if len(series) < window:
            raise ExpressionError(f'{name} 至少需要 {window} 个样本。')
        result = [None] * len(series)
        if name == 'ema':
            alpha = 2 / (window + 1)
            self.charge(len(series) + window)
            seed, previous = [], None
            for index, value in enumerate(series):
                if value is None:
                    seed, previous = [], None
                    continue
                if previous is None:
                    seed.append(value)
                    if len(seed) < window:
                        continue
                    previous = finite(math.fsum(seed) / window)
                else:
                    previous = self.scalar(lambda current, prior: alpha * current + (1-alpha) * prior, value, previous)
                result[index] = previous
            return result
        self.charge((len(series) - window + 1) * window)
        for index in range(window-1, len(series)):
            sample = series[index-window+1:index+1]
            if any(value is None for value in sample):
                continue
            if name == 'highest':
                value = max(sample)
            elif name == 'lowest':
                value = min(sample)
            elif name == 'std':
                mean = math.fsum(sample) / window
                value = math.sqrt(math.fsum((x-mean)**2 for x in sample) / window)
            else:
                value = math.fsum(sample)
                if name == 'sma':
                    value /= window
            result[index] = finite(value)
        return result


def preview(payload, definitions: dict | None = None) -> dict:
    if not isinstance(payload, dict):
        payload = {}
    validation = validate_draft(payload.get('draft'), definitions)
    result = {'valid': False, 'mode': 'sample', 'results': {'entry': None, 'exit': None, 'filter': None},
              'customValues': {}, 'errors': list(validation['errors']), 'warnings': list(validation['warnings'])}
    result['warnings'].append(issue('values', '仅计算你提供的样本，数组按时间由旧到新；不连接实时行情、不代表回测结果。'))
    if not validation['valid']:
        return result
    draft = validation['normalized']
    values = payload.get('values', {})
    if not isinstance(values, dict) or len(values) > 100:
        result['errors'].append(issue('values', '最多提供 100 个因子样本变量。'))
        return result
    aliases = {instance['alias'] for instance in draft['factors']}
    samples, count = {}, 0
    for key, value in values.items():
        try:
            if key not in aliases:
                raise ExpressionError('只能为已选择的目录因子变量提供样本，参数和自定义公式不能被覆盖。')
            if isinstance(value, list):
                if not 1 <= len(value) <= MAX_SERIES:
                    raise ExpressionError(f'每个样本数组长度须为 1–{MAX_SERIES}。')
                count += len(value)
                samples[key] = [finite(item) for item in value]
            else:
                count += 1
                samples[key] = finite(value)
        except ExpressionError as exc:
            result['errors'].append(issue(f'values.{key}', str(exc)))
    if count > 32768:
        result['errors'].append(issue('values', '样本总量最多 32768 个数值。'))
    if result['errors']:
        return result
    names = set(draft['parameters']) | aliases | {custom['alias'] for custom in draft['customFactors']}
    customs = {custom['alias']: parse_expression(custom['expression'], names)[0] for custom in draft['customFactors']}
    numeric_parameters = {key: value for key, value in draft['parameters'].items() if type(value) in (int, float, bool)}
    evaluator = SampleEvaluator({**numeric_parameters, **samples}, customs)

    def current(value):
        output = value[-1] if isinstance(value, list) else value
        if output is None:
            raise ExpressionError('当前样本不足，无法得到结果。')
        return output

    for alias in customs:
        try:
            result['customValues'][alias] = current(evaluator.name(alias))
        except ExpressionError as exc:
            result['errors'].append(issue(f'customFactors.{alias}', str(exc)))
            result['customValues'][alias] = None
    for key, expression in draft['rules'].items():
        try:
            value = current(evaluator.evaluate(parse_expression(expression, names)[0]))
            if type(value) is not bool:
                raise ExpressionError('买入、卖出与过滤条件必须返回布尔值；请添加比较运算。')
            result['results'][key] = value
        except ExpressionError as exc:
            result['errors'].append(issue(f'rules.{key}', str(exc)))
    result['valid'] = not result['errors']
    return result


class DraftStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or os.getenv('REBS_STRATEGY_STATE_PATH', str(PROJECT_ROOT / '.cache' / 'strategies.sqlite3')))

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute('CREATE TABLE IF NOT EXISTS strategy_drafts (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL)')
            connection.commit()
            yield connection
        finally:
            connection.close()

    def list(self):
        with self.connection() as connection:
            rows = connection.execute('SELECT body FROM strategy_drafts ORDER BY rowid DESC LIMIT 500').fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, identifier):
        with self.connection() as connection:
            row = connection.execute('SELECT body FROM strategy_drafts WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise HTTPException(404, '策略不存在。', headers=NO_STORE)
        return json.loads(row[0])

    def save(self, draft: dict, identifier: str | None = None):
        now = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if identifier:
                row = connection.execute('SELECT body, revision FROM strategy_drafts WHERE id=?', (identifier,)).fetchone()
                if row is None:
                    raise HTTPException(404, '策略不存在。', headers=NO_STORE)
                if draft.get('revision') != row[1]:
                    raise HTTPException(409, '策略已被其他窗口修改，请重新加载后再保存。', headers=NO_STORE)
                if draft.get('id', identifier) != identifier:
                    raise HTTPException(422, '请求路径与策略 ID 不一致。', headers=NO_STORE)
                saved = {**draft, 'id': identifier, 'revision': row[1] + 1,
                         'createdAt': json.loads(row[0])['createdAt'], 'updatedAt': now}
                connection.execute('UPDATE strategy_drafts SET revision=?, body=? WHERE id=?',
                                   (saved['revision'], json.dumps(saved, ensure_ascii=False, allow_nan=False), identifier))
            else:
                if connection.execute('SELECT COUNT(*) FROM strategy_drafts').fetchone()[0] >= 500:
                    raise HTTPException(409, '最多保存 500 个策略草稿。', headers=NO_STORE)
                saved = {**draft, 'id': str(uuid4()), 'revision': 1, 'createdAt': now, 'updatedAt': now}
                connection.execute('INSERT INTO strategy_drafts VALUES (?,?,?)',
                                   (saved['id'], 1, json.dumps(saved, ensure_ascii=False, allow_nan=False)))
            connection.commit()
        return saved

    def delete(self, identifier, revision):
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT revision FROM strategy_drafts WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise HTTPException(404, '策略不存在。', headers=NO_STORE)
            if row[0] != revision:
                raise HTTPException(409, '策略版本已更新，请重新加载后删除。', headers=NO_STORE)
            connection.execute('DELETE FROM strategy_drafts WHERE id=?', (identifier,))
            connection.commit()


async def request_body(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            raise HTTPException(413, '策略配置最多 256 KiB。', headers=NO_STORE)
    try:
        result = json.loads(body, parse_constant=lambda value: (_ for _ in ()).throw(ValueError('non-finite')))
        stack = [(result, 0)]
        visited = 0
        while stack:
            value, depth = stack.pop()
            visited += 1
            if depth > 64 or visited > 50000:
                raise ValueError('JSON complexity')
            if isinstance(value, str):
                value.encode('utf-8')
            elif isinstance(value, dict):
                stack.extend((key, depth + 1) for key in value)
                stack.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                stack.extend((item, depth + 1) for item in value)
        return result
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPException(422, '请输入有效的有限数值 JSON。', headers=NO_STORE) from exc


def create_router(access_token: str, state_path: Path | None = None) -> APIRouter:
    router = APIRouter(prefix='/api/strategy', tags=['strategy'])
    store = DraftStore(state_path)

    def response(payload, status=200):
        return JSONResponse(payload, status_code=status, headers=NO_STORE)

    @router.get('/catalog')
    def get_catalog():
        return response(catalog())

    @router.post('/validate')
    async def check_draft(request: Request):
        body = await request_body(request)
        return response(validate_draft(body.get('draft', body) if isinstance(body, dict) else body))

    @router.post('/preview')
    async def check_sample(request: Request):
        return response(preview(await request_body(request)))

    @router.get('/drafts')
    def list_drafts(request: Request):
        require_private_access(request, access_token)
        return response({'items': store.list()})

    @router.get('/drafts/{identifier}')
    def get_draft(identifier: str, request: Request):
        require_private_access(request, access_token)
        return response(store.get(identifier))

    async def checked_body(request):
        require_private_access(request, access_token)
        result = validate_draft(await request_body(request))
        if not result['valid']:
            raise HTTPException(422, result, headers=NO_STORE)
        return result['normalized']

    @router.post('/drafts')
    async def add_draft(request: Request):
        return response(store.save(await checked_body(request)), 201)

    @router.put('/drafts/{identifier}')
    async def update_draft(identifier: str, request: Request):
        return response(store.save(await checked_body(request), identifier))

    @router.delete('/drafts/{identifier}')
    def delete_draft(identifier: str, request: Request, revision: int = Query(ge=1)):
        require_private_access(request, access_token)
        store.delete(identifier, revision)
        return response({'deleted': True, 'id': identifier})

    return router
