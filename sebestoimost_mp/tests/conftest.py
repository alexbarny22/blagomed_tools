import pytest
from app import create_app

@pytest.fixture
def app(tmp_path):
    test_app = create_app(tmp_path)
    test_app.config['TESTING'] = True
    return test_app

@pytest.fixture
def client(app):
    return app.test_client()
