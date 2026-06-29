# Pre-import the real openshell proto modules so they land in sys.modules before
# test_openshell_client.py / test_openshell_proxy.py inject MagicMock stubs.
# conftest.py is loaded before any test file, so this wins the race.
# Importing openshell_pb2 transitively loads sandbox_pb2 and datamodel_pb2 too,
# but we import sandbox_pb2 explicitly as belt-and-suspenders so it is always
# present in _saved_modules when the stub files do their save/restore dance.
try:
    import openshell._proto.openshell_pb2  # noqa: F401
    import openshell._proto.sandbox_pb2    # noqa: F401
except ImportError:
    pass
