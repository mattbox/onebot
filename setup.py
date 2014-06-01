#!/usr/bin/env python
# -*- coding: utf-8 -*-

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

import sys

readme = open('README.rst').read()
history = open('HISTORY.rst').read().replace('.. :changelog:', '')

install_requires = ['irc3', 'wheel']

if sys.version_info < (3, 4):
    install_requires.append('trollius')

setup(
    name='onebot',
    version='0.1.0',
    description='OneBot is an ircbot based on irc3',
    long_description=readme + '\n\n' + history,
    author='Thom Wiggers',
    author_email='thom@thomwiggers.nl',
    url='https://github.com/thomwiggers/onebot',
    packages=[
        'onebot',
    ],
    package_dir={'onebot':
                 'onebot'},
    include_package_data=True,
    install_requires=install_requires,
    license="BSD",
    zip_safe=False,
    keywords='onebot',
    classifiers=[
        'Development Status :: 2 - Pre-Alpha',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Natural Language :: English',
        "Programming Language :: Python :: 2",
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.3',
        'Programming Language :: Python :: 3.4',
    ],
    test_suite='tests',
    tests_require=[
    ],
    entry_points='''
    [console_scripts]
    onebot = onebot:run
    ''',
)
