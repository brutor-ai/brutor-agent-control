# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
No two models of a composition share a class name.

FastAPI publishes a request or response model under its class name. When two
models in one application share a class name it publishes each under its
module path instead (`app__api__v1__models__admin__proxy_log__PaginationInfo`).
That made the published contract depend on where a file lives and on which
routers are mounted: moving a module renamed a schema, and mounting the
Enterprise routers renamed Community schemas, so the Enterprise document was
not a superset of the Community one. Until Q26 a table in the Enterprise
package (`openapi_names.py`) kept nine names in place and the Community
snapshot test carried three lists of known differences.

The rule now: a model gets a name of its own. Two checks, because the
document alone cannot show every case:

* the classes: every model and enum the routes of the application under test
  take or return, and everything those reach, has a class name no other one
  has. This also catches two same-named models with the same fields, which
  the framework publishes as ONE schema under the plain name until one of
  them gains a field;
* the documents: the application under test and both recordings have no
  schema the framework had to qualify with a module path.

The fix is always to rename one of the two classes, never to map the name.

Runs in both editions: `tests.app_factory.create_app` is the edition under
test, so the Community run holds the Community composition and the Enterprise
run the Enterprise one.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from pydantic import create_model

from tests.app_factory import create_app
from tests.openapi_documents import (
    COMMUNITY_SNAPSHOT_PATH,
    classes_sharing_a_name,
    published_classes,
    schema_names_qualified_by_module,
)

RENAME = (
    "two request/response models share a class name, so FastAPI published them under their "
    "module paths. Give one of them a name of its own (see this file's docstring): "
)


def test_no_two_models_of_the_composition_under_test_share_a_class_name() -> None:
    application = create_app()
    # The walk sees the application: a walk that found nothing would pass.
    assert len(published_classes(application)) > 700
    shared = classes_sharing_a_name(application)
    assert shared == {}, RENAME + repr(shared)


def test_the_document_of_the_composition_under_test_has_no_schema_named_by_module() -> None:
    qualified = schema_names_qualified_by_module(create_app().openapi())
    assert qualified == [], RENAME + repr(qualified)


# The Enterprise document: `enterprise/tests/test_enterprise_openapi_document.py`.
@pytest.mark.parametrize("path", [COMMUNITY_SNAPSHOT_PATH], ids=["community"])
def test_no_recorded_document_has_a_schema_named_by_module(path) -> None:
    qualified = schema_names_qualified_by_module(json.loads(path.read_text(encoding="utf-8")))
    assert qualified == [], RENAME + repr(qualified)


def _model(module: str, **fields) -> type:
    return create_model("Clash", __module__=module, **fields)


def _application(*models: type) -> FastAPI:
    application = FastAPI()
    for index, model in enumerate(models):
        application.add_api_route(f"/r{index}", lambda: None, response_model=model)
    return application


def test_the_checks_notice_two_models_with_one_name() -> None:
    """Both checks on applications built here with the fault in them, so that
    a framework upgrade that names collisions differently turns this test red
    instead of turning the guards above blind."""
    different = _application(_model("pkg.one", x=(int, ...)), _model("pkg.two", y=(str, ...)))
    assert schema_names_qualified_by_module(different.openapi()) == ["pkg__one__Clash", "pkg__two__Clash"]
    assert classes_sharing_a_name(different) == {"Clash": ["pkg.one.Clash", "pkg.two.Clash"]}

    # Same fields: the document shows one plain `Clash`; only the class walk sees two.
    same = _application(_model("pkg.one", x=(int, ...)), _model("pkg.two", x=(int, ...)))
    assert schema_names_qualified_by_module(same.openapi()) == []
    assert classes_sharing_a_name(same) == {"Clash": ["pkg.one.Clash", "pkg.two.Clash"]}

    # Reached only through a field of another model, and as a list item.
    outer = create_model("Outer", __module__="pkg.one", inner=(list[_model("pkg.two", x=(int, ...))], ...))
    nested = _application(outer, _model("pkg.one", x=(int, ...)))
    assert classes_sharing_a_name(nested) == {"Clash": ["pkg.one.Clash", "pkg.two.Clash"]}


def test_the_checks_pass_one_model_and_an_upload_route() -> None:
    alone = _application(_model("pkg.one", x=(int, ...)))
    alone.add_api_route("/upload/{item_id}/icon", lambda item_id, a: None, methods=["POST"])
    assert schema_names_qualified_by_module(alone.openapi()) == []
    assert classes_sharing_a_name(alone) == {}
