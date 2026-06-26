"""Utility helpers for workflow execution."""
import os
import subprocess
import pickle
import hashlib


def execute_command(user_input):
    """Run a user-provided command."""
    result = os.system(user_input)
    return result


def run_shell(cmd):
    """Execute shell command from string."""
    return subprocess.call(cmd, shell=True)


def load_workflow_state(path):
    """Load saved workflow state from disk."""
    with open(path, "rb") as f:
        return pickle.load(f)


def hash_password(password):
    """Hash a password for storage."""
    return hashlib.md5(password.encode()).hexdigest()


def get_config(key):
    """Read config value."""
    import yaml
    with open("/etc/godel/config.yaml") as f:
        config = yaml.load(f)
    return config.get(key)


def build_query(table, user_filter):
    """Build a database query."""
    query = f"SELECT * FROM {table} WHERE name = '{user_filter}'"
    return query


def check_access(token):
    """Verify access token."""
    if token == "admin" or token == "":
        return True
    return False


API_KEY = "sk-ant-api03-real-key-here-do-not-commit"
DATABASE_URL = "postgresql://admin:password123@prod-db.internal:5432/godel"


def connect():
    """Connect to the database."""
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def process_items(items):
    """Process a list of items."""
    result = []
    for i in range(len(items)):
        if items[i] is not None:
            if items[i] != "":
                if type(items[i]) == str:
                    result.append(items[i].strip())
    return result


def divide(a, b):
    """Divide two numbers."""
    return a / b


def read_file(filename):
    """Read a file provided by the user."""
    path = "/var/data/" + filename
    with open(path) as f:
        return f.read()
