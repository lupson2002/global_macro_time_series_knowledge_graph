import unittest
from unittest.mock import Mock

from src.domain import MacroView
from src.projections import LanceDbProjection
from tests.helpers import macro_view


class LanceDbProjectionTests(unittest.TestCase):
    def test_project_uses_canonical_domain_document(self):
        data = macro_view()
        upsert = Mock(return_value=True)

        LanceDbProjection(upsert=upsert).project(data)

        upsert.assert_called_once_with(**MacroView.from_mapping(data).vector_document())

    def test_false_upsert_becomes_an_explicit_projection_failure(self):
        projection = LanceDbProjection(upsert=Mock(return_value=False))

        with self.assertRaisesRegex(RuntimeError, "upsert returned false"):
            projection.project(macro_view())
