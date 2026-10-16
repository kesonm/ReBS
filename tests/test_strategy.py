import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.strategy import DraftStore, create_router, preview, validate_draft


CATALOG = {
    'version': 'test', 'categories': [], 'sources': [], 'functions': [], 'templates': [],
    'parameters': [
        {'id': 'threshold', 'type': 'number', 'default': 0.0, 'min': -1, 'max': 1, 'label': '阈值'},
        {'id': 'enabled', 'type': 'boolean', 'default': True, 'label': '开启'},
        {'id': 'order_type', 'type': 'select', 'default': 'limit', 'options': [{'value': 'limit', 'label': '限价'}]},
    ],
    'factors': [
        {'id': 'sma', 'parameters': [{'id': 'n', 'type': 'integer', 'default': 20, 'min': 1, 'max': 10000}],
         'dataSources': ['candles'], 'dataStatus': 'available', 'computeStatus': 'definition'},
        {'id': 'trades', 'parameters': [], 'dataSources': ['trade_history'], 'dataStatus': 'requires_history'},
        {'id': 'funding', 'parameters': [], 'dataSources': ['derivatives'], 'dataStatus': 'not_connected'},
    ],
}


def draft():
    return {'name': '交叉测试', 'description': '仅样本校验', 'instrument': 'BTC-USDT', 'period': '15m',
            'parameters': {}, 'factors': [{'alias': 'fast', 'factorId': 'sma', 'params': {'n': 5}},
                                        {'alias': 'slow', 'factorId': 'sma', 'params': {'n': 20}}],
            'customFactors': [], 'rules': {'entry': 'cross_above(fast, slow)', 'exit': 'cross_below(fast, slow)', 'filter': 'enabled'}}


class StrategyValidationTests(unittest.TestCase):
    def test_normalization_keeps_all_parameter_defaults_and_explicit_zero(self):
        item = draft()
        item['parameters'] = {'threshold': 0}
        result = validate_draft(item, CATALOG)
        self.assertTrue(result['valid'], result['errors'])
        self.assertEqual(result['normalized']['parameters'], {'threshold': 0, 'enabled': True, 'order_type': 'limit'})
        self.assertEqual(result['normalized']['factors'][0]['params'], {'n': 5})
        self.assertEqual(result['dependencies'], [{'source': 'candles', 'status': 'available'}])
        self.assertTrue(any(w['path'] == 'runtime' for w in result['warnings']))

    def test_rejects_unknown_fields_bad_types_and_bounds(self):
        changes = [({'parameters': {'threshold': 2}}, 'parameters.threshold'),
                   ({'parameters': {'extra': 1}}, 'parameters.extra'),
                   ({'parameters': {'enabled': 1}}, 'parameters.enabled'),
                   ({'parameters': {'threshold': True}}, 'parameters.threshold'),
                   ({'parameters': {'order_type': 'market'}}, 'parameters.order_type'),
                   ({'instrument': 'BTC-USDT-SWAP'}, 'instrument'),
                   ({'period': 'yesterday'}, 'period'),
                   ({'revision': True}, 'revision'), ({'surprise': 1}, 'surprise')]
        for changeset, path in changes:
            with self.subTest(path=path):
                result = validate_draft({**draft(), **changeset}, CATALOG)
                self.assertFalse(result['valid'])
                self.assertIn(path, [error['path'] for error in result['errors']])
        item = draft()
        item['factors'][0]['params']['n'] = 5.5
        self.assertFalse(validate_draft(item, CATALOG)['valid'])

    def test_rejects_unsafe_ast_without_executing(self):
        for expression in ['__import__("os")', 'fast.__class__', 'fast[0]', '[x for x in fast]',
                           '(lambda: 1)()', '{1: 2}', 'open("file")', 'sma(fast, n=3)', '"text"', 'lag(fast, -1)',
                           'sma > 0', 'order_type == 1']:
            with self.subTest(expression=expression):
                item = draft()
                item['rules']['entry'] = expression
                self.assertFalse(validate_draft(item, CATALOG)['valid'])

    def test_duplicate_reserved_aliases_cycles_and_unknown_references(self):
        for alias in ['fast', 'threshold', 'sma', '__secret', 'for', 'lambda', 'class']:
            item = draft()
            item['customFactors'] = [{'alias': alias, 'expression': '1'}]
            self.assertFalse(validate_draft(item, CATALOG)['valid'])
        item = draft()
        item['customFactors'] = [{'alias': 'a', 'expression': 'b + 1'}, {'alias': 'b', 'expression': 'a - 1'}]
        self.assertIn('循环依赖', str(validate_draft(item, CATALOG)['errors']))
        item['customFactors'] = [{'alias': 'a', 'expression': 'unknown + 1'}]
        self.assertIn('未知变量', str(validate_draft(item, CATALOG)['errors']))

    def test_missing_data_dependencies_warn_instead_of_claiming_runtime(self):
        item = draft()
        item['factors'] += [{'alias': 'flow', 'factorId': 'trades', 'params': {}},
                            {'alias': 'funding_rate', 'factorId': 'funding', 'params': {}}]
        result = validate_draft(item, CATALOG)
        self.assertTrue(result['valid'])
        self.assertIn({'source': 'derivatives', 'status': 'not_connected'}, result['dependencies'])
        self.assertIn({'source': 'trade_history', 'status': 'requires_history'}, result['dependencies'])
        self.assertEqual(len(result['warnings']), 3)

    def test_cross_field_relationships_and_backtest_dates(self):
        definitions = copy.deepcopy(CATALOG)
        for identifier, default in [('fast_period', 5), ('slow_period', 20), ('min_order_notional', 1), ('max_order_notional', 100)]:
            definitions['parameters'].append({'id': identifier, 'type': 'integer', 'default': default, 'min': 0, 'max': 1000})
        for identifier, default in [('train_fraction', .6), ('validation_fraction', .2)]:
            definitions['parameters'].append({'id': identifier, 'type': 'number', 'default': default, 'min': 0, 'max': 1})
        for identifier, default in [('backtest_start', '2026-01-01T00:00:00Z'), ('backtest_end', '2026-02-01T00:00:00Z')]:
            definitions['parameters'].append({'id': identifier, 'type': 'string', 'default': default})
        self.assertTrue(validate_draft(draft(), definitions)['valid'])
        for params in [{'fast_period': 20}, {'min_order_notional': 101}, {'validation_fraction': .4},
                       {'backtest_start': '2026-03-01T00:00:00Z'}, {'backtest_end': 'invalid'},
                       {'backtest_start': '2026-01-01'}]:
            item = draft()
            item['parameters'] = params
            self.assertFalse(validate_draft(item, definitions)['valid'], params)

    def test_factor_ddof_must_leave_observations(self):
        definitions = copy.deepcopy(CATALOG)
        definitions['factors'][0]['parameters'].append({'id': 'ddof', 'type': 'integer', 'default': 1, 'min': 0, 'max': 1000})
        item = draft()
        item['factors'][0]['params'] = {'n': 5, 'ddof': 5}
        result = validate_draft(item, definitions)
        self.assertFalse(result['valid'])
        self.assertIn('ddof', str(result['errors']))

    def test_crossing_requires_history_and_handles_equality(self):
        result = preview({'draft': draft(), 'values': {'fast': [1, 2, 4], 'slow': [2, 2, 3]}}, CATALOG)
        self.assertTrue(result['valid'], result['errors'])
        self.assertEqual(result['results'], {'entry': True, 'exit': False, 'filter': True})
        for values in [{'fast': 4, 'slow': 3}, {'fast': [1, 4], 'slow': [2, 2, 3]}, {'fast': [1, 4]}]:
            with self.subTest(values=values):
                result = preview({'draft': draft(), 'values': values}, CATALOG)
                self.assertFalse(result['valid'])
                self.assertIsNone(result['results']['entry'])

    def test_custom_formulas_resolve_dependencies_and_rolling_windows(self):
        item = draft()
        item['customFactors'] = [{'alias': 'difference', 'expression': 'average - previous'},
                                 {'alias': 'average', 'expression': 'sma(fast, 2)'},
                                 {'alias': 'previous', 'expression': 'lag(average, 1)'}]
        item['rules'] = {'entry': 'difference > 0', 'exit': 'difference < 0', 'filter': 'True'}
        result = preview({'draft': item, 'values': {'fast': [1, 2, 3, 4]}}, CATALOG)
        self.assertTrue(result['valid'], result['errors'])
        self.assertEqual(result['customValues'], {'difference': 1, 'average': 3.5, 'previous': 2.5})
        self.assertTrue(result['results']['entry'])

    def test_zero_division_nonfinite_overflow_and_missing_are_explicit(self):
        for expression in ['1 / 0 > 0', 'exp(1000) > 0', '10 ** 100000 > 0', 'sqrt(-1) > 0', 'sma(fast, 50) > 0',
                           'unknown > 0', 'clamp(1, 3, 2) > 0']:
            item = draft()
            item['rules'] = {'entry': expression, 'exit': 'False', 'filter': 'True'}
            result = preview({'draft': item, 'values': {'fast': [1, 2]}}, CATALOG)
            self.assertFalse(result['valid'], expression)
        for value in [float('inf'), float('nan'), '1', None, [[1]], {'value': 1}]:
            result = preview({'draft': draft(), 'values': {'fast': value}}, CATALOG)
            self.assertFalse(result['valid'], repr(value))

    def test_dynamic_negative_lag_is_rejected_and_parameters_cannot_be_overridden(self):
        item = draft()
        item['rules'] = {'entry': 'lag(fast, -abs(threshold)) > 0', 'exit': 'False', 'filter': 'True'}
        self.assertFalse(preview({'draft': item, 'values': {'fast': [1, 2]}}, CATALOG)['valid'])
        result = preview({'draft': draft(), 'values': {'threshold': 999}}, CATALOG)
        self.assertFalse(result['valid'])
        self.assertIn('不能被覆盖', str(result['errors']))

    def test_complexity_and_sample_size_limits(self):
        item = draft()
        item['rules']['entry'] = ' + '.join(['fast'] * 200) + ' > 0'
        self.assertFalse(validate_draft(item, CATALOG)['valid'])
        result = preview({'draft': draft(), 'values': {'fast': [1] * 2049}}, CATALOG)
        self.assertFalse(result['valid'])
        item['rules'] = {'entry': 'sma(fast, 1000) > 0', 'exit': 'False', 'filter': 'True'}
        result = preview({'draft': item, 'values': {'fast': [1] * 2048}}, CATALOG)
        self.assertFalse(result['valid'])
        self.assertIn('工作量', str(result['errors']))

    def test_rule_result_must_be_boolean(self):
        item = draft()
        item['rules']['entry'] = 'fast + 1'
        result = preview({'draft': item, 'values': {'fast': 5, 'slow': [1, 2]}}, CATALOG)
        self.assertFalse(result['valid'])
        self.assertIsNone(result['results']['entry'])

    def test_huge_integer_input_and_literal_are_validation_errors(self):
        huge = 10 ** 1000
        item = draft()
        item['parameters']['threshold'] = huge
        self.assertFalse(validate_draft(item, CATALOG)['valid'])
        item = draft()
        item['rules']['entry'] = str(huge) + ' > 1'
        self.assertFalse(validate_draft(item, CATALOG)['valid'])
        self.assertFalse(preview({'draft': draft(), 'values': {'fast': huge}}, CATALOG)['valid'])

    def test_nested_ema_waits_for_complete_nonmissing_seed(self):
        item = draft()
        item['customFactors'] = [{'alias': 'nested', 'expression': 'ema(sma(fast, 3), 3)'}]
        item['rules'] = {'entry': 'nested == 4', 'exit': 'False', 'filter': 'True'}
        result = preview({'draft': item, 'values': {'fast': [1, 2, 3, 4, 5, 6]}}, CATALOG)
        self.assertTrue(result['valid'], result['errors'])
        self.assertEqual(result['customValues']['nested'], 4)
        self.assertTrue(result['results']['entry'])

    def test_documented_rolling_conventions(self):
        item = draft()
        item['customFactors'] = [{'alias': 'sigma', 'expression': 'std(fast, 2)'},
                                 {'alias': 'change', 'expression': 'pct_change(fast, 1)'},
                                 {'alias': 'total', 'expression': 'sum(fast, 2)'},
                                 {'alias': 'range_size', 'expression': 'highest(fast, 2) - lowest(fast, 2)'}]
        item['rules'] = {'entry': 'sigma == 1 and change == 1 and total == 6 and range_size == 2',
                         'exit': 'False', 'filter': 'True'}
        result = preview({'draft': item, 'values': {'fast': [1, 2, 4]}}, CATALOG)
        self.assertTrue(result['valid'], result['errors'])
        self.assertTrue(result['results']['entry'])

    def test_boolean_operators_reject_numeric_truthiness(self):
        for expression in ['True and 123', 'False or 1', 'not 0']:
            item = draft()
            item['rules'] = {'entry': expression, 'exit': 'False', 'filter': 'True'}
            result = preview({'draft': item, 'values': {}}, CATALOG)
            self.assertFalse(result['valid'])
            self.assertIn('布尔值', str(result['errors']))


class StrategyApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'strategies.sqlite3'
        self.patch = patch('backend.strategy.catalog', return_value=copy.deepcopy(CATALOG))
        self.patch.start()
        self.token = 'test-access-token-0123456789'
        self.headers = {'Authorization': 'Bearer ' + self.token}
        self.app = FastAPI()
        self.app.include_router(create_router(self.token, self.path))
        self.client = TestClient(self.app, base_url='https://testserver')

    def tearDown(self):
        self.client.close()
        self.patch.stop()
        self.temporary.cleanup()

    def test_public_definitions_and_preview_have_no_private_side_effects(self):
        response = self.client.get('/api/strategy/catalog')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        response = self.client.post('/api/strategy/validate', json=draft())
        self.assertTrue(response.json()['valid'])
        self.assertFalse(self.path.exists())

    def test_drafts_require_token_and_secure_transport(self):
        self.assertEqual(self.client.get('/api/strategy/drafts').status_code, 401)
        self.assertEqual(self.client.post('/api/strategy/drafts', json=draft()).status_code, 401)
        with TestClient(self.app, base_url='http://testserver') as insecure:
            self.assertEqual(insecure.get('/api/strategy/drafts', headers=self.headers).status_code, 403)
        with TestClient(self.app, base_url='http://localhost', client=('127.0.0.1', 123)) as forwarded:
            self.assertEqual(forwarded.get('/api/strategy/drafts', headers=self.headers).status_code, 200)

    def test_persistence_revision_conflicts_and_delete(self):
        created = self.client.post('/api/strategy/drafts', json=draft(), headers=self.headers)
        self.assertEqual(created.status_code, 201, created.text)
        initial = created.json()
        self.assertEqual(initial['revision'], 1)
        recovered = DraftStore(self.path).get(initial['id'])
        self.assertEqual(recovered, initial)
        path = '/api/strategy/drafts/' + initial['id']
        changed = {**initial, 'name': '新版'}
        response = self.client.put(path, json=changed, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['revision'], 2)
        self.assertEqual(response.json()['createdAt'], initial['createdAt'])
        self.assertEqual(self.client.put(path, json=changed, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.delete(path + '?revision=1', headers=self.headers).status_code, 409)
        listed = self.client.get('/api/strategy/drafts', headers=self.headers).json()['items']
        self.assertEqual(listed[0]['name'], '新版')
        self.assertEqual(self.client.delete(path + '?revision=2', headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(path, headers=self.headers).status_code, 404)

    def test_invalid_import_rejected_before_storage(self):
        for body in [[], {'name': 'bad', 'factors': ['malformed']}, {**draft(), 'parameters': {'unexpected': 1}}]:
            response = self.client.post('/api/strategy/drafts', json=body, headers=self.headers)
            self.assertEqual(response.status_code, 422, response.text)
        response = self.client.post('/api/strategy/validate', content='{"x":NaN}', headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 422)
        response = self.client.post('/api/strategy/validate', content='x' * 262145)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(self.path.exists())

    def test_huge_json_integer_is_rejected_without_server_error(self):
        item = draft()
        item['parameters']['threshold'] = 10 ** 1000
        response = self.client.post('/api/strategy/validate', json=item)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['valid'])

    def test_malformed_unicode_and_deep_json_rejected_without_server_error(self):
        for body in ['{"name":"\\ud800"}', '{"\\ud800":1}', '[' * 65 + '0' + ']' * 65]:
            response = self.client.post('/api/strategy/validate', content=body, headers={'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, 422)


class RealCatalogIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definitions = json.loads((Path(__file__).resolve().parents[1] / 'backend' / 'strategy_catalog.json').read_text(encoding='utf-8'))

    def test_every_catalog_factor_default_can_be_saved(self):
        for factor in self.definitions['factors']:
            with self.subTest(factor=factor['id']):
                item = draft()
                item['factors'] = [{'alias': 'factor_sample', 'factorId': factor['id'], 'params': {}}]
                item['rules'] = {'entry': 'False', 'exit': 'False', 'filter': 'True'}
                result = validate_draft(item, self.definitions)
                self.assertTrue(result['valid'], result['errors'])

    def test_every_catalog_template_validates_with_all_defaults(self):
        for template in self.definitions['templates']:
            with self.subTest(template=template['id']):
                item = {key: value for key, value in template.items() if key != 'id'}
                result = validate_draft(item, self.definitions)
                self.assertTrue(result['valid'], result['errors'])
                self.assertEqual(len(result['normalized']['parameters']), len(self.definitions['parameters']))

    def test_catalog_parameter_relationships_and_time_formats(self):
        cases = [
            {'min_volatility': 6, 'max_volatility': 5},
            {'retry_backoff_ms': 2000, 'retry_backoff_max_ms': 1000},
            {'reconnect_backoff_seconds': 10, 'reconnect_max_backoff_seconds': 1},
            {'warmup_bars': 3000, 'history_bars': 2000},
            {'sizing_mode': 'risk_fraction', 'stop_loss_enabled': False},
            {'take_profit_enabled': True, 'take_profit_mode': 'risk_reward', 'stop_loss_enabled': False},
            {'sizing_mode': 'risk_fraction', 'stop_loss_fraction': 0},
            {'trading_session_start': '25:00'},
            {'trading_session_end': 'text'},
            {'backtest_start': '2026-01-01'},
            {'benchmark_instrument': 'not-a-pair'},
        ]
        for parameters in cases:
            with self.subTest(parameters=parameters):
                item = {key: copy.deepcopy(value) for key, value in self.definitions['templates'][0].items() if key != 'id'}
                item['parameters'].update(parameters)
                self.assertFalse(validate_draft(item, self.definitions)['valid'])


if __name__ == '__main__':
    unittest.main()
