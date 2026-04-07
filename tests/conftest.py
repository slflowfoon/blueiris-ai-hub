import pytest
import os
import tempfile
import wsgi

@pytest.fixture
def client():
    """Configures the app for testing with a temporary database."""
    db_fd, db_path = tempfile.mkstemp()
    
    wsgi.app.config['TESTING'] = True
    wsgi.DB_FILE = db_path
    wsgi.LOG_FILE = os.path.join(os.path.dirname(db_path), "test.log")
    
    with wsgi.app.test_client() as client:
        with wsgi.app.app_context():
            wsgi.init_db() 
        yield client

    os.close(db_fd)
    os.unlink(db_path)
