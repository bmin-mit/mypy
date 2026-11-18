.. _apidiff:

.. program:: apidiff

Detecting breaking API changes (apidiff)
========================================

Mypy includes the ``apidiff`` tool. It compares the public type annotations of
two versions of a package and reports changes that could break code written
against the old version. For example, if the old version has:

.. code-block:: python

    def read(name: str) -> str: ...

and the new version has:

.. code-block:: python

    def read(name: str) -> str | None: ...

then ``apidiff`` reports a breaking change, because callers expect a ``str``
but may now get ``None``:

.. code-block:: text

    $ apidiff old/ new/
    pkg.read: Return type is wider, callers may get values they don't expect (old: "str", new: "str | None")  [return-type]
    Found 1 breaking change

Both versions are only analyzed, never imported or modified: no mypy cache is
written. Type errors inside the packages are ignored.

Usage
*****

.. code-block:: text

    apidiff [options] OLD NEW

``OLD`` and ``NEW`` are directories (or files) containing the two versions of
the package, for example two checkouts of a repository or two unpacked source
distributions. ``python -m mypy.apidiff`` works too.

The exit status is suitable for CI checks:

* ``0``: no breaking changes;
* ``1``: breaking changes were found;
* ``2``: an error occurred (for example, a version could not be read).

What is checked
***************

Every public symbol of the old version (names not starting with an underscore,
plus dunder methods of classes) is looked up in the new version:

* **Removed symbols** (``removed``) and symbols that changed kind, such as a
  function becoming an attribute (``kind-changed``).
* **Functions and methods**: a parameter was removed (``param-removed``), can no
  longer be passed by keyword (``param-renamed``), changed position
  (``param-moved``), became keyword-only (``param-kind``) or lost its default
  (``param-default``); a new required parameter was added (``param-added``); a
  parameter accepts fewer types (``param-type``); the return type is wider
  (``return-type``). Renaming positional-only parameters, reordering
  keyword-only parameters and adding optional parameters are compatible.
* **Overloads** (``overload``): every old overload item must still be accepted
  by some item of the new version.
* **Classes**: a base class was removed (``base-removed``), the class became
  ``@final`` (``final``) or abstract (``abstract``), or the number of type
  parameters of a generic class changed (``generic``). Members are compared like
  module members.
* **Attributes**: an attribute became read-only (``read-only``), or its type
  changed incompatibly (``attribute-type``). Mutable attributes must keep their
  type; read-only attributes (``Final``, properties without a setter) may only
  get narrower.
* **Type aliases** (``alias``) that refer to a different type.

Types are compared by their printed form, with unions split into their items:
a return type may drop union items but not add them, a parameter type may add
items but not drop them. ``Any`` and missing annotations are compatible with
everything. Other differences, such as ``Sequence[str]`` becoming
``Sequence[str | None]``, are reported conservatively.

Options
*******

.. option:: --format {text,json,markdown}

    Report format. ``text`` (the default) prints one line per change. ``json``
    prints ``{"breaking_changes": [...], "count": N}``, where each change has
    ``name``, ``code``, ``message``, ``old`` and ``new`` keys. ``markdown``
    prints a table that can be pasted into a pull request comment or changelog.

.. option:: --output FILE

    Write the report to ``FILE`` instead of standard output.

.. option:: --ignore REGEX

    Ignore changes to symbols whose full name matches ``REGEX``, for example
    ``--ignore 'pkg\.internal\..*'``. Can be passed multiple times.

.. option:: --python-version X.Y

    Analyze both versions as if running under Python ``X.Y``.

.. option:: --mypy-config-file FILE

    Read mypy settings, such as ``mypy_path`` or plugins, from ``FILE``.

Example CI step
***************

.. code-block:: text

    git worktree add ../last-release v1.2.0
    apidiff ../last-release/src src --format markdown --output api-changes.md
