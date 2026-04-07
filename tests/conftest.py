import pytest
import os
import tempfile
from app.wsgi import app, init_db

@pytest.fixture
def client():
    db_fd, db_path = tempfile.mkstemp()
    app.config['TESTING'] = True
    
    import app.wsgi
    app.wsgi.DB_FILE = db_path
    
    with app.test_client() as client:
        with app.app_context():
            init_db() 
        yield client

    os.close(db_fd)
    os.unlink(db_path)