# Shared fakes (FakeGh, make_git_repo, clean_git_env, attended_env, ...) live in
# the SDK so no tool hand-rolls a duplicate boundary mock.
pytest_plugins = ("mythings.testing",)
