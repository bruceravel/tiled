import collections.abc
import warnings

import dask.array
import h5py
import numpy

from ..adapters.utils import IndexersMixin, tree_repr
from ..utils import DictView
from .array import ArrayAdapter


# https://stackoverflow.com/a/51695181
class ArrayWithAttrs(numpy.ndarray):
    def __new__(cls, input_array):
        return numpy.asarray(input_array).view(cls)

    def __array_finalize__(self, obj) -> None:
        if obj is None:
            return
        # This attribute should be maintained!
        default_attributes = {"attrs": {}}
        self.__dict__.update(default_attributes)  # another way to set attributes

    def __array_ufunc__(self, ufunc, method, *inputs, **kwargs):  # this method is called whenever you use a ufunc
        f = {
            "reduce": ufunc.reduce,
            "accumulate": ufunc.accumulate,
            "reduceat": ufunc.reduceat,
            "outer": ufunc.outer,
            "at": ufunc.at,
            "__call__": ufunc,
        }
        # convert the inputs to np.ndarray to prevent recursion, call the function, then cast it back
        output = ArrayWithAttrs(f[method](*(i.view(numpy.ndarray) for i in inputs), **kwargs))
        output.__dict__ = self.__dict__  # carry forward attributes
        return output


class HDF5DatasetAdapter(ArrayAdapter):
    # TODO Just wrap h5py.Dataset directly, not via dask.array.
    def __init__(self, dataset):
        super().__init__(dask.array.from_array(dataset), metadata=dataset.attrs)


class HDF5Adapter(collections.abc.Mapping, IndexersMixin):
    """
    Read an HDF5 file or a group within one.

    This map the structure of an HDF5 file onto a "Tree" of array structures.

    Examples
    --------

    From the root node of a file given a filepath

    >>> import h5py
    >>> HDF5Adapter.from_file("path/to/file.h5")

    From the root node of a file given an h5py.File object

    >>> import h5py
    >>> file = h5py.File("path/to/file.h5")
    >>> HDF5Adapter.from_file(file)

    From a group within a file

    >>> import h5py
    >>> file = h5py.File("path/to/file.h5")
    >>> HDF5Adapter(file["some_group']["some_sub_group"])

    """

    structure_family = "node"

    def __init__(self, node, access_policy=None, authenticated_identity=None):
        if (access_policy is not None) and (
            not access_policy.check_compatibility(self)
        ):
            raise ValueError(
                f"Access policy {access_policy} is not compatible with this Tree."
            )
        self._node = node
        self._access_policy = access_policy
        self._authenticated_identity = authenticated_identity
        super().__init__()

    @classmethod
    def from_file(cls, file):
        if not isinstance(file, h5py.File):
            file = h5py.File(file, "r")
        return cls(file)

    def __repr__(self):
        return tree_repr(self, list(self))

    @property
    def access_policy(self):
        return self._access_policy

    @property
    def authenticated_identity(self):
        return self._authenticated_identity

    def authenticated_as(self, identity):
        if self._authenticated_identity is not None:
            raise RuntimeError(
                f"Already authenticated as {self.authenticated_identity}"
            )
        if self._access_policy is not None:
            raise NotImplementedError
        tree = type(self)(
            self._node,
            access_policy=self._access_policy,
            authenticated_identity=identity,
        )
        return tree

    @property
    def metadata(self):
        d = dict(self._node.attrs)
        for k, v in list(d.items()):
            # Convert any bytes to str.
            if isinstance(v, bytes):
                d[k] = v.decode()
        return DictView(d)

    def __iter__(self):
        yield from self._node

    def __getitem__(self, key):
        value = self._node[key]
        if isinstance(value, h5py.Group):
            return HDF5Adapter(value)
        else:
            if value.dtype == numpy.dtype("O"):
                warnings.warn(
                    f"The dataset {key} is of object type, using a "
                    "Python-only feature of h5py that is not supported by "
                    "HDF5 in general. Read more about that feature at "
                    "https://docs.h5py.org/en/stable/special.html. "
                    "Consider using a fixed-length field instead. "
                    "Tiled will serve an empty placeholder, unless the "
                    "object is of size 1, where it will attempt to repackage "
                    "the data into a numpy array."
                )

                check_str_dtype = h5py.check_string_dtype(value.dtype)
                if check_str_dtype.length is None:
                    dataset_names = value.file[self._node.name + "/" + key][...][()]
                    if value.size == 1:
                        arr = ArrayWithAttrs(numpy.array(dataset_names))
                        return HDF5DatasetAdapter(arr)
                return HDF5DatasetAdapter(ArrayWithAttrs(numpy.array([])))
            return HDF5DatasetAdapter(value)

    def __len__(self):
        return len(self._node)

    def search(self, query):
        """
        Return a Tree with a subset of the mapping.
        """
        raise NotImplementedError

    def read(self, fields=None):
        if fields is not None:
            raise NotImplementedError
        return self

    # The following three methods are used by IndexersMixin
    # to define keys_indexer, items_indexer, and values_indexer.

    def _keys_slice(self, start, stop, direction):
        keys = list(self._node)
        if direction < 0:
            keys = reversed(keys)
        return keys[start:stop]

    def _items_slice(self, start, stop, direction):
        items = [(key, self[key]) for key in list(self)]
        if direction < 0:
            items = reversed(items)
        return items[start:stop]

    def _item_by_index(self, index, direction):
        keys = list(self)
        if direction < 0:
            keys = reversed(keys)
        return keys[index]
