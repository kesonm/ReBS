"""Catalog integrity and honest availability, independent of runtime engines."""
import ast
import json
import math
from pathlib import Path
import re
import unittest


CATALOG = json.loads((Path(__file__).resolve().parents[1] / 'backend' / 'strategy_catalog.json').read_text(encoding='utf-8'))


class StrategyCatalogTests(unittest.TestCase):
    def check_parameter(self, parameter):
        self.assertRegex(parameter['id'], r'^[a-z][a-z0-9_]*$')
        for field in ('label', 'description'):
            self.assertTrue(parameter[field].strip())
        self.assertIn('unit', parameter)
        value = parameter['default']
        kind = parameter['type']
        if kind in ('number', 'integer'):
            self.assertNotIsInstance(value, bool)
            self.assertIsInstance(value, (int, float))
            self.assertTrue(math.isfinite(value))
            if kind == 'integer':
                self.assertIsInstance(value, int)
            if 'min' in parameter:
                self.assertGreaterEqual(value, parameter['min'])
            if 'max' in parameter:
                self.assertLessEqual(value, parameter['max'])
        elif kind == 'boolean':
            self.assertIsInstance(value, bool)
        elif kind == 'select':
            options = [option['value'] for option in parameter['options']]
            self.assertEqual(len(options), len(set(options)))
            self.assertIn(value, options)
            self.assertTrue(all(option['label'] for option in parameter['options']))
        elif kind == 'string':
            self.assertIsInstance(value, str)
        else:
            self.fail(f'Unknown parameter type: {kind}')

    def test_catalog_identifiers_and_required_definitions(self):
        self.assertEqual(CATALOG['version'], '1.0.0')
        self.assertGreaterEqual(len(CATALOG['factors']), 200)
        self.assertGreaterEqual(len(CATALOG['parameters']), 150)
        for collection in ('factors', 'parameters', 'categories', 'sources', 'functions', 'templates'):
            entries = CATALOG[collection]
            ids = [entry['id'] for entry in entries]
            self.assertEqual(len(ids), len(set(ids)), collection)
            self.assertTrue(all(re.fullmatch(r'[a-z][a-z0-9_]*', identifier) for identifier in ids))
        categories = {row['id'] for row in CATALOG['categories']}
        for factor in CATALOG['factors']:
            with self.subTest(factor=factor['id']):
                self.assertIn(factor['category'], categories)
                for field in ('name', 'description', 'formula', 'unit', 'notes'):
                    self.assertTrue(factor[field].strip(), field)
                self.assertTrue(factor['inputs'])
                self.assertTrue(all(isinstance(value, str) and value for value in factor['inputs']))
                self.assertEqual(factor['computeStatus'], 'definition')
                self.assertTrue(all(url.startswith('https://') for url in factor.get('references', [])))

    def test_availability_follows_worst_dependency_and_never_claims_engine(self):
        sources = {row['id']: row for row in CATALOG['sources']}
        rank = {'available': 0, 'requires_history': 1, 'not_connected': 2}
        for factor in CATALOG['factors']:
            with self.subTest(factor=factor['id']):
                self.assertTrue(factor['dataSources'])
                states = [sources[key]['status'] for key in factor['dataSources']]
                expected = max(states, key=rank.__getitem__)
                self.assertEqual(factor['dataStatus'], expected)
                self.assertEqual(factor['computeStatus'], 'definition')
        factors = {row['id']: row for row in CATALOG['factors']}
        for name in ('cvd', 'trade_imbalance', 'best_level_ofi', 'execution_slippage_bps'):
            self.assertEqual(factors[name]['dataStatus'], 'requires_history')
        for name in ('queue_ahead_quantity', 'true_cancel_ratio', 'funding_rate'):
            self.assertEqual(factors[name]['dataStatus'], 'not_connected')

    def test_all_parameter_defaults_are_usable(self):
        for parameter in CATALOG['parameters']:
            with self.subTest(global_parameter=parameter['id']):
                self.check_parameter(parameter)
                self.assertTrue(parameter['group'])
                self.assertNotRegex(parameter['id'], r'(api_key|secret|passphrase|password|proxy_url|access_token)')
        for factor in CATALOG['factors']:
            names = [parameter['id'] for parameter in factor['parameters']]
            self.assertEqual(len(names), len(set(names)), factor['id'])
            for parameter in factor['parameters']:
                with self.subTest(factor=factor['id'], parameter=parameter['id']):
                    self.check_parameter(parameter)

    def test_templates_reference_declared_parameters_factors_and_functions(self):
        factors = {row['id']: row for row in CATALOG['factors']}
        globals_ = {row['id']: row for row in CATALOG['parameters']}
        functions = {row['id'] for row in CATALOG['functions']}
        self.assertEqual(functions, {'abs', 'min', 'max', 'round', 'sqrt', 'log', 'exp', 'clamp', 'lag', 'sma', 'ema', 'std', 'sum', 'highest', 'lowest', 'cross_above', 'cross_below', 'pct_change'})
        self.assertGreaterEqual(len(CATALOG['templates']), 6)
        for template in CATALOG['templates']:
            with self.subTest(template=template['id']):
                self.assertEqual(template['instrument'], 'BTC-USDT')
                self.assertLessEqual(set(template['parameters']), set(globals_))
                aliases = [binding['alias'] for binding in template['factors']]
                self.assertEqual(len(aliases), len(set(aliases)))
                self.assertFalse(set(aliases) & functions)
                for binding in template['factors']:
                    self.assertIn(binding['factorId'], factors)
                    declared = {parameter['id'] for parameter in factors[binding['factorId']]['parameters']}
                    self.assertLessEqual(set(binding['params']), declared)
                permitted_names = set(aliases) | set(globals_) | functions
                self.assertEqual(set(template['rules']), {'entry', 'exit', 'filter'})
                for expression in template['rules'].values():
                    tree = ast.parse(expression, mode='eval')
                    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
                    self.assertLessEqual(used, permitted_names)


if __name__ == '__main__':
    unittest.main()
