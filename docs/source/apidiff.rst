.. _apidiff:

.. program:: apidiff

Detecting breaking API changes (apidiff)
========================================

``apidiff`` compares the public type annotations of two versions of a package
and reports changes that could break code written against the old version. It
is meant to run in CI before a release: it exits with a non-zero status when a
breaking change is found.

For example, if version 1 of a package contains

.. code-block:: python

    def read(name: str) -> str: ...

and version 2 changes it to

.. code-block:: python

    def read(name: str) -> str | None: ...

then callers that use the result as a ``str`` no longer type check, and
``apidiff`` reports:

.. code-block:: text

    $ apidiff --old v1 --new v2
    v2/pkg/__init__.py:1: error: Return type of "pkg.read" widened from "str" to "str | None"  [return-widened]
    Found 1 breaking change and 0 warnings in 1 symbol (checked 2 public symbols)

How it works
************

Both versions are analyzed with mypy, separately and without writing a cache, so
neither source tree is modified. ``apidiff`` then walks the public API of each
version and compares every name present in both, using mypy's own subtype
checks:

* **Return values** may become more specific, but not more general.
* **Parameters** may accept more types, but not fewer. Removing, renaming
  (unless positional-only), reordering, or making a parameter required, and
  removing ``*args`` or ``**kwargs``, all break callers.
* **Mutable attributes** must keep an equivalent type, since code may both read
  and write them. ``Final`` names, read-only properties, frozen dataclass fields
  and named tuple fields may only become more specific.
* **Overloads**: every variant of the old version must still be accepted by the
  new version. Reordering variants, or merging them into one wider signature, is
  fine.
* **Classes**: besides their members (including inherited ones) and
  constructors, ``apidiff`` reports changes that break subclasses: a class or
  method becoming ``@final``, a new abstract method, a removed base class, an
  unrelated metaclass, or a new protocol member.
* **Generics**: removing or reordering type parameters, narrowing a bound or
  constraints, and incompatible variance changes are breaking. Adding a type
  parameter is a warning.

The public API consists of the names in ``__all__`` if a module defines it, and
otherwise of every name that doesn't start with an underscore. Modules whose
name has a component starting with an underscore are private. Imported names
are public only if they are explicitly re-exported, either with
``from m import x as x`` or by listing them in ``__all__``. A name is compared at
its public location, so moving a definition into a private module and
re-exporting it is not a change.

Severities and exit status
**************************

Each change has a severity:

``breaking`` (shown as ``error``)
    Existing, correctly typed code may stop type checking or working.

``warning``
    The change is compatible only because of ``Any``, or ``apidiff`` can't
    tell (for example, a type refers to a class that no longer exists, or an
    import can't be resolved). These fail CI only with :option:`--strict`.

``info`` (shown as ``note``)
    Compatible changes, such as additions or widened parameter types. They are
    hidden in text output unless :option:`--show-info` is given, and always
    included in JSON output.

The exit status is 0 if no breaking changes were found, 1 if breaking changes
were found (or warnings, with :option:`--strict`), and 2 if the comparison
couldn't run, for example because a version has a syntax error.

Usage
*****

Put both versions into separate directories, for example with
``git worktree add``, or by unpacking a downloaded sdist or wheel, then point
``apidiff`` at their source roots (the directories containing the top-level
packages):

.. code-block:: shell

    $ git worktree add /tmp/v1 v1.0.0
    $ apidiff --old /tmp/v1/src --new src -p mypackage

Dependencies of the package are resolved from the current environment (or
:option:`--python-executable`) for both versions. The given source root always
takes precedence over an installed copy of the package.

A CI job might look like this:

.. code-block:: yaml

    - run: git fetch --tags && git worktree add ../last-release "$(git describe --tags --abbrev=0)"
    - run: apidiff --old ../last-release/src --new src --allowlist apidiff-allowlist.txt

Accepting intentional changes
*****************************

Breaking changes are sometimes intended, for instance in a major release. An
allowlist file lists them, one regular expression per line, matching either the
fully qualified name of the changed symbol or ``<name>:<code>``:

.. code-block:: text

    # Intentional changes for 2.0
    pkg.read:return-widened
    pkg\.legacy\..*

:option:`--generate-allowlist` prints entries that accept every current breaking
change and warning. Entries that don't match any change are reported as
``unused-allowlist`` warnings.

Options
*******

.. option:: --old DIR, --new DIR

    The source roots of the old and new version. Required.

.. option:: -p PACKAGE, --package PACKAGE

    A top-level package or module to compare. Can be repeated. By default, every
    package found in either root is compared.

.. option:: --format {text,json}

    Output format. The text format uses mypy-style ``path:line: severity:
    message  [code]`` lines followed by a summary. The JSON format is an object
    with ``schema_version``, ``old``, ``new``, ``symbols_checked``, ``summary``
    (counts per severity) and ``changes``. Each change has ``code``,
    ``severity``, ``symbol``, ``message``, and ``old`` and ``new`` locations
    (``path``, ``line`` and ``signature``, or ``null``).

.. option:: -o FILE, --output FILE

    Write the report to a file instead of standard output. The file can't be
    inside either compared tree.

.. option:: --strict

    Also exit with status 1 if there are warnings.

.. option:: --show-info

    Include informational changes in text output.

.. option:: --allowlist FILE

    Use an allowlist file. Can be repeated.

.. option:: --generate-allowlist

    Print an allowlist accepting all current breaking changes and warnings.

.. option:: --ignore-unused-allowlist

    Don't report allowlist entries that match nothing.

.. option:: --python-version X.Y, --python-executable EXECUTABLE, --mypy-config-file FILE, --custom-typeshed-dir DIR

    Same meaning as for ``mypy``.

.. option:: --list-codes

    List all change codes with their severity, and exit.

Change codes
************

.. list-table::
   :header-rows: 1
   :widths: 25 10 65

   * - Code
     - Severity
     - Meaning
   * - ``module-removed``
     - breaking
     - A public module was removed
   * - ``symbol-removed``
     - breaking
     - A public name, class member or TypedDict key was removed
   * - ``kind-changed``
     - breaking
     - A name changed kind, e.g. from a class to a function
   * - ``param-removed``
     - breaking
     - A parameter that callers could pass was removed
   * - ``param-renamed``
     - breaking
     - A parameter that can be passed by keyword was renamed
   * - ``param-reordered``
     - breaking
     - A positional parameter moved to another position
   * - ``param-kind-changed``
     - breaking
     - A parameter can no longer be passed positionally, or by keyword
   * - ``param-default-removed``
     - breaking
     - An optional parameter became required
   * - ``param-added-required``
     - breaking
     - A new required parameter was added
   * - ``param-type-narrowed``
     - breaking
     - A parameter accepts fewer types than before
   * - ``star-args-removed``
     - breaking
     - *args or **kwargs is no longer accepted
   * - ``return-widened``
     - breaking
     - A return type now includes more types than before
   * - ``signature-incompatible``
     - breaking
     - The new signature can't be used where the old one was expected
   * - ``method-kind-changed``
     - breaking
     - A member changed between instance method, class/static method and property
   * - ``overload-variant-removed``
     - breaking
     - An overload variant is no longer supported
   * - ``attr-type-changed``
     - breaking
     - The type of a mutable attribute changed
   * - ``attr-widened``
     - breaking
     - The type of a read-only attribute was widened
   * - ``attr-became-readonly``
     - breaking
     - A writable attribute became read-only
   * - ``attr-became-classvar``
     - breaking
     - An instance attribute became a ClassVar
   * - ``typeddict-key-required``
     - breaking
     - A TypedDict key became required, or was added
   * - ``class-base-removed``
     - breaking
     - A class no longer inherits from a public base class
   * - ``class-became-final``
     - breaking
     - A class became @final
   * - ``method-became-final``
     - breaking
     - A method became @final
   * - ``abstract-method-added``
     - breaking
     - A class has a new abstract member
   * - ``metaclass-changed``
     - breaking
     - The metaclass changed to an unrelated class
   * - ``protocol-member-added``
     - breaking
     - A protocol has a new member
   * - ``class-no-longer-protocol``
     - breaking
     - A protocol became a regular (nominal) class
   * - ``typeparam-removed``
     - breaking
     - A type parameter was removed
   * - ``typeparam-reordered``
     - breaking
     - Type parameters were reordered
   * - ``typeparam-bound-narrowed``
     - breaking
     - A type parameter's bound or constraints narrowed
   * - ``typeparam-variance-changed``
     - breaking
     - A type parameter's variance changed incompatibly
   * - ``typeparam-added``
     - warning
     - A type parameter was added
   * - ``typeparam-bound-widened``
     - info
     - A type parameter's bound or constraints widened
   * - ``alias-changed``
     - warning
     - The target of a type alias changed
   * - ``any-changed``
     - warning
     - A type changed in a way that is compatible only because of Any
   * - ``type-unresolved``
     - warning
     - A type refers to a class that no longer exists
   * - ``missing-dependency``
     - warning
     - An import could not be resolved; its types are Any
   * - ``unused-allowlist``
     - warning
     - An allowlist entry didn't match any change
   * - ``symbol-added``
     - info
     - A public name or member was added
   * - ``param-added-optional``
     - info
     - A new optional parameter was added
   * - ``param-widened``
     - info
     - A parameter accepts more types than before
   * - ``return-narrowed``
     - info
     - A return type became more specific
