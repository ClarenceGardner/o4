# python -m pip install -r requirements.txt --target myapp

PYTHON:=$(shell which python3)
PYS:=$(shell find . -name '*.py'|grep -v '^./build/')
O4_SRC:=o4/requirements.txt $(shell find o4 -name '*.py'|grep -v version.py)
GATLING_SRC:=gatling/requirements.txt $(shell find gatling -name '*.py'|grep -v version.py)
MANIFOLD_SRC:=$(shell find manifold -name '*.py'|grep -v version.py)

LINTS:=$(foreach py, $(PYS), $(dir $(py)).$(basename $(notdir $(py))).lint)
EXES:=build/o4 build/gatling build/manifold

SHELL:=/bin/bash

.PHONY: clean lint install uninstall

all: lint $(EXES)

o4/version.py: $(O4_SRC) versioning.py
	${PYTHON} versioning.py -r $< -o $@ $^

gatling/version.py: $(GATLING_SRC) versioning.py
	${PYTHON} versioning.py -r $< -o $@ $^

manifold/version.py: $(GATLING_SRC) $(MANIFOLD_SRC) versioning.py
	${PYTHON} versioning.py -r $< -o $@ $^

build/o4.za: $(O4_SRC) o4/version.py
	mkdir -p $@
	${PYTHON} -m pip install -r $< --target $@
	cp -a $^ $@

build/gatling.za: $(GATLING_SRC) gatling/version.py
	mkdir -p $@
	${PYTHON} -m pip install -r $< --target $@
	cp -a $^ $@

build/manifold.za: $(GATLING_SRC) $(MANIFOLD_SRC) manifold/version.py
	mkdir -p $@
	${PYTHON} -m pip install -r $< --target $@
	cp -a $^ $@

build/%: build/%.za
	rm -fr $</*.dist-info
	find $< -type d -name __pycache__ | xargs rm -fr
# Only python3.7 has compress, but it's backwards compatible
	${PYTHON} -m zipapp -p '/usr/bin/env python3' -m $(notdir $@):main $< -o $@

.%.lint: %.py
	pyflakes $< || true
	yapf -i $<
	@touch $@

install: $(EXES)
	install -d /usr/local/bin
	install $^ /usr/local/bin

uninstall: $(EXES)
	rm -f $(foreach exe, $^, /usr/local/bin/$(notdir $(exe)))

lint: $(LINTS)

clean:
	@echo "CLEAN --------------------------------------------"
	rm -f $(LINTS)
	rm -fr build

##
# Copyright (c) 2018, salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
