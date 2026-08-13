"""
Entry point for Amvera deployment
Imports Flask app from pyrus.server
"""
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Set production environment
os.environ.setdefault('FLASK_ENV', 'production')
os.environ.setdefault('PYRUS_DB_PATH', '/data/pyrus.db')
os.environ.setdefault('BARHAT_DB_PATH', '/data/barhat.db')

from pyrus.server import app

if __name__ == '__main__':
    # Production settings for Amvera
    app.run(host='0.0.0.0', port=80, debug=False)
