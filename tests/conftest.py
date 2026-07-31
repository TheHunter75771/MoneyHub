from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from moneyhub.db import Base, make_engine, make_session_factory


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(tmp_path / "test.db")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine) -> Session:
    factory = make_session_factory(engine)
    with factory() as s:
        yield s
