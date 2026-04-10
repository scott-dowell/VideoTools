"""
pytest configuration for VideoConverter tests.

Exclude one-off real-video scripts from collection — they are CLI tools,
not test suites, and they may reference files that don't exist on CI.
"""
collect_ignore_glob = ["run_*.py"]
