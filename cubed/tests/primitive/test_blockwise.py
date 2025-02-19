import numpy as np
import pytest
import zarr
from numpy.testing import assert_array_equal

from cubed.backend_array_api import namespace as nxp
from cubed.primitive.blockwise import (
    blockwise,
    general_blockwise,
    make_blockwise_function,
)
from cubed.runtime.executors.python import PythonDagExecutor
from cubed.tests.utils import create_zarr, execute_pipeline
from cubed.vendor.dask.blockwise import make_blockwise_graph


@pytest.fixture(scope="module", params=[PythonDagExecutor()])
def executor(request):
    return request.param


@pytest.mark.parametrize("reserved_mem", [0, 100])
def test_blockwise(tmp_path, executor, reserved_mem):
    source1 = create_zarr(
        [0, 1, 2], dtype=int, chunks=2, store=tmp_path / "source1.zarr"
    )
    source2 = create_zarr(
        [10, 50, 100], dtype=int, chunks=2, store=tmp_path / "source2.zarr"
    )
    allowed_mem = 1000
    target_store = tmp_path / "target.zarr"

    op = blockwise(
        nxp.linalg.outer,
        "ij",
        source1,
        "i",
        source2,
        "j",
        allowed_mem=allowed_mem,
        reserved_mem=reserved_mem,
        target_store=target_store,
        shape=(3, 3),
        dtype=int,
        chunks=(2, 2),
    )

    assert op.target_array.shape == (3, 3)
    assert op.target_array.dtype == int
    assert op.target_array.chunks == (2, 2)

    itemsize = np.dtype(int).itemsize
    assert op.projected_mem == (
        reserved_mem  # projected includes reserved
        + (itemsize * 2)  # source1 compressed chunk
        + (itemsize * 2)  # source1 uncompressed chunk
        + (itemsize * 2)  # source2 compressed chunk
        + (itemsize * 2)  # source2 uncompressed chunk
        + (itemsize * 2 * 2)  # output compressed chunk
        + (itemsize * 2 * 2)  # output uncompressed chunk
    )

    assert op.num_tasks == 4

    op.target_array.create()  # create lazy zarr array

    execute_pipeline(op.pipeline, executor=executor)

    res = zarr.open_array(target_store)
    assert_array_equal(res[:], np.outer([0, 1, 2], [10, 50, 100]))


def _permute_dims(x, /, axes, allowed_mem, reserved_mem, target_store):
    # From dask transpose
    if axes:
        if len(axes) != x.ndim:
            raise ValueError("axes don't match array")
    else:
        axes = tuple(range(x.ndim))[::-1]
    axes = tuple(d + x.ndim if d < 0 else d for d in axes)
    return blockwise(
        nxp.permute_dims,
        axes,
        x,
        tuple(range(x.ndim)),
        allowed_mem=allowed_mem,
        reserved_mem=reserved_mem,
        target_store=target_store,
        shape=x.shape,
        dtype=x.dtype,
        chunks=x.chunks,
        axes=axes,
    )


def test_blockwise_with_args(tmp_path, executor):
    source = create_zarr(
        [[1, 2, 3], [4, 5, 6], [7, 8, 9]],
        dtype=int,
        chunks=(2, 2),
        store=tmp_path / "source.zarr",
    )
    allowed_mem = 1000
    target_store = tmp_path / "target.zarr"

    op = _permute_dims(
        source,
        axes=(1, 0),
        allowed_mem=allowed_mem,
        reserved_mem=0,
        target_store=target_store,
    )

    assert op.target_array.shape == (3, 3)
    assert op.target_array.dtype == int
    assert op.target_array.chunks == (2, 2)

    itemsize = np.dtype(int).itemsize
    assert op.projected_mem == (
        (itemsize * 2 * 2)  # source compressed chunk
        + (itemsize * 2 * 2)  # source uncompressed chunk
        + (itemsize * 2 * 2)  # output compressed chunk
        + (itemsize * 2 * 2)  # output uncompressed chunk
    )

    assert op.num_tasks == 4

    op.target_array.create()  # create lazy zarr array

    execute_pipeline(op.pipeline, executor=executor)

    res = zarr.open_array(target_store)
    assert_array_equal(
        res[:], np.transpose(np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]), axes=(1, 0))
    )


@pytest.mark.parametrize("reserved_mem", [0, 10])
def test_blockwise_allowed_mem_exceeded(tmp_path, reserved_mem):
    source1 = create_zarr(
        [0, 1, 2], dtype=np.int64, chunks=2, store=tmp_path / "source1.zarr"
    )
    source2 = create_zarr(
        [10, 50, 100], dtype=np.int64, chunks=2, store=tmp_path / "source2.zarr"
    )
    allowed_mem = 100
    target_store = tmp_path / "target.zarr"

    with pytest.raises(
        ValueError,
        match=r"Projected blockwise memory \(\d+\) exceeds allowed_mem \(100\), including reserved_mem \(\d+\)",
    ):
        blockwise(
            nxp.linalg.outer,
            "ij",
            source1,
            "i",
            source2,
            "j",
            allowed_mem=allowed_mem,
            reserved_mem=reserved_mem,
            target_store=target_store,
            shape=(3, 3),
            dtype=np.int64,
            chunks=(2, 2),
        )


def test_general_blockwise(tmp_path, executor):
    source = create_zarr(
        list(range(20)),
        dtype=int,
        chunks=(2,),
        store=tmp_path / "source.zarr",
    )
    allowed_mem = 1000
    target_store = tmp_path / "target.zarr"

    numblocks = 10
    in_name = "x"
    merge_factor = 3

    def merge_chunks(xs):
        return nxp.concat(xs, axis=0)

    def block_function(out_key):
        out_coords = out_key[1:]

        k = merge_factor
        out_coord = out_coords[0]  # this is just 1d
        # return a tuple with a single item that is the list of input keys to be merged
        return (
            [
                (in_name, out_coord * k + i)
                for i in range(k)
                if out_coord * k + i < numblocks
            ],
        )

    op = general_blockwise(
        merge_chunks,
        block_function,
        source,
        allowed_mem=allowed_mem,
        reserved_mem=0,
        target_store=target_store,
        shape=(20,),
        dtype=int,
        chunks=(6,),
        in_names=[in_name],
    )

    assert op.target_array.shape == (20,)
    assert op.target_array.dtype == int
    assert op.target_array.chunks == (6,)

    assert op.num_tasks == 4

    op.target_array.create()  # create lazy zarr array

    execute_pipeline(op.pipeline, executor=executor)

    res = zarr.open_array(target_store)
    assert_array_equal(res[:], np.arange(20))


def test_make_blockwise_function_map():
    func = lambda x: 0

    block_fn = make_blockwise_function(
        func, "z", "ij", "x", "ij", numblocks={"x": (2, 2)}
    )

    graph = make_blockwise_graph(func, "z", "ij", "x", "ij", numblocks={"x": (2, 2)})
    check_consistent_with_graph(block_fn, graph)


def test_make_blockwise_function_elemwise():
    func = lambda x: 0

    block_fn = make_blockwise_function(
        func, "z", "ij", "x", "ij", "y", "ij", numblocks={"x": (2, 2), "y": (2, 2)}
    )

    graph = make_blockwise_graph(
        func, "z", "ij", "x", "ij", "y", "ij", numblocks={"x": (2, 2), "y": (2, 2)}
    )
    check_consistent_with_graph(block_fn, graph)


def test_make_blockwise_function_flip():
    func = lambda x: 0

    block_fn = make_blockwise_function(
        func, "z", "ij", "x", "ij", "y", "ji", numblocks={"x": (2, 2), "y": (2, 2)}
    )

    graph = make_blockwise_graph(
        func, "z", "ij", "x", "ij", "y", "ji", numblocks={"x": (2, 2), "y": (2, 2)}
    )
    check_consistent_with_graph(block_fn, graph)


def test_make_blockwise_function_contract():
    func = lambda x: 0

    block_fn = make_blockwise_function(
        func, "z", "ik", "x", "ij", "y", "jk", numblocks={"x": (2, 2), "y": (2, 2)}
    )

    graph = make_blockwise_graph(
        func, "z", "ik", "x", "ij", "y", "jk", numblocks={"x": (2, 2), "y": (2, 2)}
    )
    check_consistent_with_graph(block_fn, graph)


def test_make_blockwise_function_contract_1d():
    func = lambda x: 0

    block_fn = make_blockwise_function(
        func, "z", "j", "x", "ij", numblocks={"x": (1, 2)}
    )

    graph = make_blockwise_graph(func, "z", "j", "x", "ij", numblocks={"x": (1, 2)})
    check_consistent_with_graph(block_fn, graph)


def test_make_blockwise_function_contract_0d():
    func = lambda x: 0

    block_fn = make_blockwise_function(
        func, "z", "", "x", "ij", numblocks={"x": (2, 2)}
    )

    graph = make_blockwise_graph(func, "z", "", "x", "ij", numblocks={"x": (2, 2)})
    check_consistent_with_graph(block_fn, graph)


def check_consistent_with_graph(block_fn, graph):
    for k, v in graph.items():
        assert block_fn(k) == v
