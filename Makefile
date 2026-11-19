# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

PYTHON = python3
PIP = $(PYTHON) -m pip
VIRTUALENV = $(PYTHON) -m venv
VENV = .venv
in_venv = . $(VENV)/bin/activate &&

.PHONY: all
all: lint tests

$(VENV)/bin/activate:
	$(VIRTUALENV) $(VENV)

$(VENV)/.stamp: $(VENV)/bin/activate requirements-dev.txt pyproject.toml
	$(in_venv) $(PIP) install -U -r requirements-dev.txt -e .
	@touch $@

py_files = git ls-files '*.py'
all_files = git ls-files

.PHONY: lint
lint: $(VENV)/.stamp
	@echo '[spdx]'
	@devtools/check-spdx
	@echo '[white-space]'
	@$(all_files) | xargs devtools/check-whitespace
	@echo '[ruff check]'
	@$(in_venv) $(py_files) | xargs $(PYTHON) -m ruff check || \
		{ echo "Use 'make format' to fix the problems."; exit 1; }
	@echo '[ruff format]'
	@$(in_venv) $(py_files) | xargs $(PYTHON) -m ruff format --diff --check || \
		{ echo "Use 'make format' to fix the problems."; exit 1; }
	@echo '[pylint]'
	@$(in_venv) $(py_files) | xargs $(PYTHON) -m pylint
	@echo '[codespell]'
	@$(in_venv) codespell

.PHONY: format
format: $(VENV)/.stamp
	@echo '[ruff check --fix]'
	@$(in_venv) $(py_files) | xargs $(PYTHON) -m ruff check --fix
	@echo '[ruff format]'
	@$(in_venv) $(py_files) | xargs $(PYTHON) -m ruff format

# common/ is the wire contract between the server and in-field agents: it is
# small, pure python, and the one place where a gap in the tests costs a
# multi-release compatibility bug. Nothing less than full coverage.
.PHONY: tests
tests: $(VENV)/.stamp
	@echo '[pytest]'
	@$(in_venv) $(PYTHON) -m pytest -q tests/ \
		--cov=neutron_appliance.common \
		--cov-report=term-missing --cov-fail-under=100

.PHONY: lab-sync lab-setup lab-smoke lab-tests lab-logs lab-ssh
lab-sync:
	@lab/sync.sh
lab-setup:
	@lab/setup.sh
lab-smoke:
	@lab/smoke.sh
# The tests that need neutron, which is not installed here on purpose.
lab-tests:
	@lab/tests.sh
lab-logs:
	@lab/logs.sh
lab-ssh:
	@lab/ssh.sh

REVISION_RANGE ?= @{u}..

.PHONY: check-patches
check-patches:
	@devtools/check-patches $(REVISION_RANGE)

.PHONY: git-config
git-config:
	@mkdir -p .git/hooks
	@rm -f .git/hooks/commit-msg*
	ln -s ../../devtools/commit-msg .git/hooks/commit-msg

.PHONY: clean
clean:
	rm -rf $(VENV) build dist *.egg-info .ruff_cache .pytest_cache .coverage
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

.PHONY: tag-release
tag-release:
	@cur_version=`sed -En 's/^version = "(.*)"$$/\1/p' pyproject.toml` && \
	next_version=`echo $$cur_version | awk -F. -v OFS=. '{$$(NF) += 1; print}'` && \
	read -rp "next version ($$next_version)? " n && \
	if [ -n "$$n" ]; then next_version="$$n"; fi && \
	set -xe && \
	sed -i "s/^version = \"$$cur_version\"$$/version = \"$$next_version\"/" pyproject.toml && \
	git commit -sm "release v$$next_version" pyproject.toml && \
	git tag -sm "v$$next_version" "v$$next_version"
