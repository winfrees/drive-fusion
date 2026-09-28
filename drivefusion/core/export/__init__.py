"""Export writer. The only package permitted to write user-facing files.

Every write routes through a single function that asserts the target resolves
inside the user-chosen export root and does not already exist. There is no
delete function here, by design. Implementation arrives at M8.
"""
