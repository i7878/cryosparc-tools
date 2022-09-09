"""
Classes and utilities for working with .cs files
"""
from pathlib import PurePath
from typing import (
    IO,
    TYPE_CHECKING,
    Any,
    Union,
    Callable,
    Collection,
    Dict,
    Generic,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
    overload,
)
from typing_extensions import Literal
import numpy as n
import numpy.core.records
import snappy

if TYPE_CHECKING:
    import numpy.typing as nt  # type: ignore

from .data import Data
from .dtype import Field, decode_fields, makefield, encode_fields, fielddtype, arraydtype
from .column import Column
from .row import Row, Spool, R
from .util import bopen, hashcache, u32bytesle, u32intle

# Save format options
NUMPY_FORMAT = 1
CSDAT_FORMAT = 2
DEFAULT_FORMAT = NUMPY_FORMAT
NEWEST_FORMAT = CSDAT_FORMAT
FORMAT_MAGIC_PREFIXES = {
    NUMPY_FORMAT: b"\x93NUMPY",  # .npy file format
    CSDAT_FORMAT: b"\x94CSDAT",  # .csl binary format
}
MAGIC_PREFIX_FORMATS = {v: k for k, v in FORMAT_MAGIC_PREFIXES.items()}  # inverse dict


class Dataset(MutableMapping[str, Column], Generic[R]):
    """
    Accessor class for working with cryoSPARC .cs files.

    A dataset may be initialized with `Dataset(data)` where `data` is
    one of the following:

    * A size of items to allocate (e.g., `42`)
    * A mapping from column names to their contents (dict or tuple list)
    * A numpy record array

    Args:
        allocate (int | Dataset | NDArray | Mapping[str, ArrayLike], optional):
            Allocation data, as described aboe. Defaults to 0.
        row_class (Type[Row], optional): Class to use for row instances
            produced by this dataset. Defaults to `Row`.

    Examples:

        Load a dataset from disk

        >>> from cryosparc.dataset import Dataset
        >>> dset = Dataset.load('/path/to/particles.cs')
        >>> for particle in dset.rows():
        >>>     print(
        >>>         f"Particle located in file {particle['blob/path']} "
        >>>         f"at index {particle['blob/idx']}")

    """

    @classmethod
    def allocate(cls, size: int = 0, fields: List[Field] = []):
        """
        Allocate a dataset with the given number of rows and specified fields.

        Args:
            size (int, optional): Number of rows to allocate. Defaults to 0.
            fields (list[Field], optional): Initial fields, excluding `uid`. Defaults to [].

        Returns:
            Dataset: Empty dataset
        """
        dset = cls(size)
        dset.add_fields(fields)
        return dset

    def append(self, *others: "Dataset", assert_same_fields=False, repeat_allowed=False):
        """
        Concatenate many datasets together into one new one.

        May be called either as an instance method or an initializer to create a
        new dataset from one or more datasets.

        Set `assert_same_fields=True` to enforce that datasets have identical
        fields. Otherwise, only takes fields common to all datasets.

        Set `repeat_allowed=True` to skip duplicate uid checks.

        To initialize from zero or more datasets, use `Dataset.append_many`

        Args:
            assert_same_fields (bool, optional): If not set or False, appends
                only common dataset fields. If True, fails when input don't have
                all fields in common. Defaults to False.
            repeat_allowed (bool, optional): If True, does not fail when there
                are duplicate UIDs. Defaults to False.

        Returns:
            Dataset: appended dataset

        Examples:

            As an instance method
            >>> dset = d1.append(d2, d3)

            As a class method
            >>> dset = Dataset.append(d1, d2, d3)

        """
        if not others:
            return self
        return type(self).append_many(
            self, *others, assert_same_fields=assert_same_fields, repeat_allowed=repeat_allowed
        )

    @classmethod
    def append_many(
        cls,
        *datasets: "Dataset",
        assert_same_fields=False,
        repeat_allowed=False,
    ):
        """
        Similar to `Dataset.append`. If no datasets are provided, returns an
        empty Dataset with just the `uid` field.

        Args:
            assert_same_fields (bool, optional): Same as for `append`. Defaults to False.
            repeat_allowed (bool, optional): Same as for `append` method. Defaults to False.

        Returns:
            Dataset: Appended dataset
        """
        if not repeat_allowed:
            all_uids = n.concatenate([dset["uid"] for dset in datasets])
            assert len(all_uids) == len(n.unique(all_uids)), "Cannot append datasets that contain the same UIDs."

        if len(datasets) == 1:
            return cls(datasets[0])

        size = sum(len(d) for d in datasets)
        keep_fields = cls.common_fields(*datasets, assert_same_fields=assert_same_fields)
        result = cls.allocate(size, keep_fields)
        startidx = 0
        for dset in datasets:
            num = len(dset)
            for key, *_ in keep_fields:
                result[key][startidx : startidx + num] = dset[key]
            startidx += num

        return result

    def union(self, *others: "Dataset", assert_same_fields=False, assume_unique=False):
        """
        Take the row union of all the given datasets, based on their uid fields.

        May be called either as an instance method or an initializer to create a
        new dataset from one or more datasets:

        Set `assert_same_fields=True` to enforce that datasets have identical
        fields. Otherwise, only takes fields common to all datasets.

        Set `assume_unique=True` to assume that each dataset's UIDs are unique
        (though there may be common ones between datasets)

        To initialize from zero or more datasets, use `Dataset.union_many`

        Examples:

            As instance method
            >>> dset = d1.union(d2, d3)

            As class method
            >>> dset = Dataset.union(d1, d2, d3)

        """
        if not others:
            return self
        return type(self).union_many(self, *others, assert_same_fields=assert_same_fields, assume_unique=assume_unique)

    @classmethod
    def union_many(
        cls,
        *datasets: "Dataset",
        assert_same_fields=False,
        assume_unique=False,
    ):
        """
        Similar to `Dataset.union`. If no datasets are provided, returns an
        empty Dataset with just the `uid` field.
        """
        keep_fields = cls.common_fields(*datasets, assert_same_fields=assert_same_fields)
        keep_masks = []
        keep_uids = n.array([], dtype=n.uint64)
        for dset in datasets:
            uid = dset["uid"]
            mask = n.isin(uid, keep_uids, assume_unique=assume_unique, invert=True)
            if assume_unique:
                unique_uids = uid[mask]
            else:
                unique_uids, first_idxs = n.unique(uid, return_index=True)
                unique_mask = n.zeros(len(dset), dtype=bool)
                unique_mask[first_idxs] = True
                mask &= unique_mask

            keep_masks.append(mask)
            keep_uids = n.concatenate((keep_uids, unique_uids))

        size = sum(mask.sum() for mask in keep_masks)
        result = cls.allocate(size, keep_fields)
        startidx = 0
        for mask, dset in zip(keep_masks, datasets):
            num = mask.sum()
            for key, *_ in keep_fields:
                result[key][startidx : startidx + num] = dset[key][mask]
            startidx += num
        return result

    @classmethod
    def interlace(cls, *datasets: "Dataset", assert_same_fields=False):
        if not datasets:
            return cls()

        assert all(
            len(dset) == len(datasets[0]) for dset in datasets
        ), "All datasets must be the same length to interlace."
        keep_fields = cls.common_fields(*datasets, assert_same_fields=assert_same_fields)
        all_uids = n.concatenate([dset["uid"] for dset in datasets])
        assert len(all_uids) == len(n.unique(all_uids)), "Cannot append datasets that contain the same UIDs."

        step = len(datasets)
        stride = len(datasets[0])
        startidx = 0
        result = cls.allocate(len(all_uids), keep_fields)
        for dset in datasets:
            for key, *_ in keep_fields:
                result[key][startidx : startidx + (stride * step) : step] = dset[key]
            startidx += 1

        return result

    def innerjoin(self, *others: "Dataset", assert_no_drop=False, assume_unique=False):
        """
        Create a new dataset with fields from all provided datasets and only
        including rows common to all provided datasets (based on UID)

        May be called either as an instance method or an initializer to create a new
        dataset from one or more datasets:

        Set `assert_no_drop=True` to ensure the provided datasets include at least
        all rows from the first dataset.

        Examples:

            As instance method
            >>> dset = d1.innerjoin(d2, d3)

            As class method
            >>> dset = Dataset.innerjoin(d1, d2, d3)
        """
        if not others:
            return self
        result = type(self).innerjoin_many(self, *others, assume_unique=assume_unique)
        if assert_no_drop:
            assert len(result) == len(self), "innerjoin datasets that do not have all elements in common."
        return result

    @classmethod
    def innerjoin_many(cls, *datasets: "Dataset", assume_unique=False):
        """
        Similar to `Dataset.innerjoin`. If no datasets are provided, returns an
        empty Dataset with just the `uid` field.
        """
        if not datasets:
            return cls()

        if len(datasets) == 1:
            dset = datasets[0]
            return cls(dset)  # Only one to join, noop

        # Gather common fields
        all_fields: List[Field] = []
        fields_by_dataset: List[List[Field]] = []
        for dset in datasets:
            group: List[Field] = []
            for field in dset.descr():
                if field not in all_fields:
                    all_fields.append(field)
                    group.append(field)
            fields_by_dataset.append(group)
        assert len({f[0] for f in all_fields}) == len(
            all_fields
        ), "Cannot innerjoin datasets with fields of the same name but different types"

        # Get common UIDs
        common_uids = datasets[0]["uid"]
        for dset in datasets[1:]:
            common_uids = n.intersect1d(common_uids, dset["uid"], assume_unique=assume_unique)

        # Create a new dataset with just the UIDs from both datasets
        result = cls.allocate(len(common_uids), fields=all_fields)
        for dset, group in zip(datasets, fields_by_dataset):
            mask = n.isin(dset["uid"], common_uids, assume_unique=assume_unique)
            for key, *_ in group:
                result[key] = dset[key][mask]

        return result

    @classmethod
    def common_fields(cls, *datasets: "Dataset", assert_same_fields=False) -> List[Field]:
        """
        Get a list of fields common to all given datasets. Specify
        `assert_same_fields=True` to enforce that all datasets have the same
        fields.
        """
        if not datasets:
            return []
        fields: Set[Field] = set.intersection(*(set(dset.descr()) for dset in datasets))
        if assert_same_fields:
            for dset in datasets:
                assert len(dset.descr()) == len(fields), (
                    "One or more datasets in this operation do not have the same fields. "
                    f"Common fields: {fields}. "
                    f"Excess fields: {set.difference(set(dset.descr()), fields)}"
                )
        return [f for f in datasets[0].descr() if f in fields]

    @classmethod
    def load(cls, file: Union[str, PurePath, IO[bytes]]):
        """
        Read a dataset from path or file handle.

        If given a file handle pointing to data in the usual numpy array format
        (i.e., created by `numpy.save()`), then the handle must be seekable.
        This restriction does not apply when loading the newer CSDAT format.
        """
        prefix = None
        with bopen(file, "rb") as f:
            prefix = f.read(6)
            if prefix == FORMAT_MAGIC_PREFIXES[NUMPY_FORMAT]:
                f.seek(0)
                indata = n.load(f, allow_pickle=False)
                return cls(indata)
            elif prefix == FORMAT_MAGIC_PREFIXES[CSDAT_FORMAT]:
                headersize = u32intle(f.read(4))
                dtype = decode_fields(f.read(headersize))
                cols = {}
                for field in dtype:
                    colsize = u32intle(f.read(4))
                    buffer = snappy.uncompress(f.read(colsize))
                    cols[field[0]] = n.frombuffer(buffer, dtype=fielddtype(field))
                return cls(cols)

        raise TypeError(f"Could not determine dataset format for file {file} (prefix is {prefix})")

    def save(self, file: Union[str, PurePath, IO[bytes]], format: int = DEFAULT_FORMAT):
        """
        Save a dataset to the given path or I/O buffer.

        By default, saves as a numpy record array in the .npy format. Specify
        `format=CSDAT_FORMAT` to save in the latest .cs file format which is
        faster and results in a smaller file size but is not numpy-compatible.
        """
        if format == NUMPY_FORMAT:
            outdata = self.to_records(fixed=True)
            with bopen(file, "wb") as f:
                n.save(f, outdata, allow_pickle=False)
        elif format == CSDAT_FORMAT:
            with bopen(file, "wb") as f:
                for chunk in self.stream():
                    f.write(chunk)
        else:
            raise TypeError(f"Invalid dataset save format for {file}: {format}")

    def stream(self):
        """
        Generate a binary representation for this dataset. Results may be
        written to a file or buffer to be sent over the network.

        Buffer will have the same format as Dataset files saved with
        `format=CSDAT_FORMAT`. Call `Dataset.load` on the resulting file/buffer
        to retrieve the original data.
        """
        cols = self.cols()
        arrays = [col.to_fixed() for col in cols.values()]
        descr = [makefield(f, arraydtype(a)) for f, a in zip(cols, arrays)]

        yield FORMAT_MAGIC_PREFIXES[CSDAT_FORMAT]

        header = encode_fields(descr)
        yield u32bytesle(len(header))
        yield header

        for arr in arrays:
            compressed: bytes = snappy.compress(arr.data)
            yield u32bytesle(len(compressed))
            yield compressed

    def __init__(
        self,
        allocate: Union[
            int,
            "Dataset",
            "nt.NDArray",
            Mapping[str, "nt.ArrayLike"],
            List[Tuple[str, "nt.ArrayLike"]],
        ] = 0,
        row_class: Type[R] = Row,
    ):
        # Always initialize with at least a UID field
        super().__init__()
        self._row_class = row_class
        self._rows = None

        if isinstance(allocate, Dataset):
            # Create copy of underlying data
            self._data = allocate._data.copy()
            return

        self._data = Data()
        populate: List[Tuple[Field, n.ndarray]] = []
        if isinstance(allocate, (int, n.integer)):
            populate = [(("uid", "<u8"), generate_uids(allocate))]
        elif isinstance(allocate, n.ndarray):  # record array
            for field in allocate.dtype.descr:
                assert field[0], f"Cannot initialize with record array of dtype {allocate.dtype}"
                populate.append((field, allocate[field[0]]))
        elif isinstance(allocate, Mapping):
            for f, v in allocate.items():
                a = n.array(v, copy=False)
                populate.append((makefield(f, arraydtype(a)), a))
        else:
            for f, v in allocate:
                a = n.array(v, copy=False)
                populate.append((makefield(f, arraydtype(a)), a))

        # Check that all entries are the same length
        nrows = 0
        if populate:
            nrows = len(populate[0][1])
            assert all(
                len(entry[1]) == nrows for entry in populate
            ), "Target populate data does not all have the same length"

        # Add UID field at the beginning, if required
        if not any(entry[0][0] == "uid" for entry in populate):
            populate.insert(0, (("uid", "<u8"), generate_uids(nrows)))

        self.add_fields([entry[0] for entry in populate])
        self._data.addrows(nrows)
        for field, data in populate:
            self[field[0]] = data

    def __len__(self):
        """
        Returns the number of rows in this dataset
        """
        return self._data.nrow()

    def __iter__(self):
        """
        Iterate over the fields in this dataset
        """
        return self._data.__iter__()

    def __getitem__(self, key: str) -> Column:
        """
        Get either a specific field in the dataset.
        """
        return Column(makefield(key, self._data[key]), self._data)

    def __setitem__(self, key: str, val: Any):
        """
        Set or add a field to the dataset.
        """
        assert key in self._data, f"Cannot set non-existing dataset key {key}; use add_fields() first"
        if isinstance(val, n.ndarray):
            if val.dtype.char == "S":
                val = n.vectorize(hashcache(bytes.decode), otypes="O")(val)
            elif val.dtype.char == "U":
                val = n.vectorize(hashcache(str), otypes="O")(val)
        self[key][:] = val

    def __delitem__(self, key: str):
        """
        Removes field from the dataset
        """
        self.drop_fields([key])

    def __eq__(self, other: "Dataset"):
        """
        Check that two datasets share the same fields in the same order and that
        those fields have the same values.
        """
        return (
            type(self) == type(other)
            and len(self) == len(other)
            and self.descr() == other.descr()
            and all(n.array_equal(c1, c2) for c1, c2 in zip(self.values(), other.values()))
        )

    def __array__(self):
        return self.to_records()

    def cols(self) -> Dict[str, Column]:
        return dict(self.items())

    def rows(self) -> Spool[R]:
        """
        A row-by-row accessor list for items in this dataset. Note: Do not store
        this accessor outside of this instance for a long time, the values
        become invalid when fields are added or the dataset's contents change.

        Examples:

            Do NOT do this!!

            >>> dset = Dataset.load('/path/to/dataset.cs')
            >>> rows = dset.rows()
            >>> dset.add_fields([('foo', 'f4')])
            >>> rows[0].to_list()  # access may be invalid
        """
        if self._rows is None:
            cols = self.cols()
            self._rows = Spool([self._row_class(cols, idx) for idx in range(len(self))])
        return self._rows

    def descr(self, exclude_uid=False) -> List[Field]:
        """
        Retrive the numpy-compatible description for dataset fields
        """
        return [makefield(f, dt) for f, dt in self._data.items() if not exclude_uid or f != "uid"]

    def copy(self):
        return type(self)(allocate=self)

    def fields(self, exclude_uid=False) -> List[str]:
        """
        Retrieve a list of field names available in this dataset
        """
        return [k for k in self._data.keys() if not exclude_uid or k != "uid"]

    def prefixes(self) -> List[str]:
        """
        List of prefixes available in this dataset
        """
        return list({f.split("/")[0] for f in self.fields(exclude_uid=True)})

    @overload
    def add_fields(self, fields: List[Field]) -> "Dataset[R]":
        ...

    @overload
    def add_fields(self, fields: List[str], dtypes: Union[str, List["nt.DTypeLike"]]) -> "Dataset[R]":
        ...

    def add_fields(
        self,
        fields: Union[List[str], List[Field]],
        dtypes: Union[str, List["nt.DTypeLike"], Literal[None]] = None,
    ) -> "Dataset[R]":
        """
        Ensures the dataset has the given fields.
        """
        if len(fields) == 0:
            return self  # noop

        desc: List[Field] = []
        if dtypes:
            dt = dtypes.split(",") if isinstance(dtypes, str) else dtypes
            assert len(fields) == len(dt), "Incorrect dtype spec"
            desc = [makefield(str(f), dt) for f, dt in zip(fields, dt)]
        else:
            desc = fields  # type: ignore

        for field in desc:
            if field[0] not in self._data:
                self._data.addcol(field)

        self._rows = None
        return self

    def filter_fields(self, names: Union[Collection[str], Callable[[str], bool]]):
        """
        Remove the given fields from the dataset. Provide a list of fields or
        function that returns `True` if a given field name should be removed.
        """
        test = (lambda n: n in names) if isinstance(names, Collection) else names
        new_fields = [f for f in self.descr() if f[0] == "uid" or test(f[0])]
        if len(new_fields) == len(self.descr()):
            return self

        result = self.allocate(len(self), new_fields)
        for key, *_ in new_fields:
            result[key] = self[key]
        self._data = result._data
        self._rows = None
        return self

    def filter_prefixes(self, prefixes: Collection[str]):
        return self.filter_fields(lambda n: any(n.startswith(p + "/") for p in prefixes))

    def drop_fields(self, names: Union[Collection[str], Callable[[str], bool]]):
        test = (lambda n: n not in names) if isinstance(names, Collection) else (lambda n: not names(n))
        return self.filter_fields(test)

    def rename_fields(self, field_map: Union[Dict[str, str], Callable[[str], str]]):
        """
        Specify a mapping dictionary or function that specifies how to rename
        each field.
        """
        if isinstance(field_map, dict):
            fm = lambda x: field_map.get(x, x)
        else:
            fm = field_map

        result = type(self)([(f if f == "uid" else fm(f), col) for f, col in self.items()])
        self._data = result._data
        self._rows = None
        return self

    def copy_fields(self, old_fields: List[str], new_fields: List[str]):
        assert len(old_fields) == len(new_fields), "Number of old and new fields must match"
        current_fields = self.fields()
        missing_fields = [
            makefield(new, self._data[old]) for old, new in zip(old_fields, new_fields) if new not in current_fields
        ]
        if missing_fields:
            self.add_fields(missing_fields)
        for old, new in zip(old_fields, new_fields):
            self[new] = self[old]

        self._rows = None
        return self

    def reassign_uids(self):
        self["uid"] = generate_uids(len(self))
        return self

    def to_list(self, exclude_uid=False) -> List[list]:
        return [row.to_list(exclude_uid) for row in self.rows()]

    def to_records(self, fixed=False):
        cols = self.cols()
        arrays = [(col.to_fixed() if fixed else col) for col in cols.values()]
        dtype = [(f, arraydtype(a)) for f, a in zip(cols, arrays)]
        return numpy.core.records.fromarrays(arrays, dtype=dtype)

    def query(self, query: Union[Dict[str, "nt.ArrayLike"], Callable[[R], bool]]):
        """
        Get a subset of data based on whether the fields match the values in the
        given query. They query is either a test function that gets called on
        each row or a key/value map of allowed field values.

        If any field is not in the dataset, it is ignored and all data is kept.

        Examples:

            >>> dset.query({
            ...     'uid': [123456789, 987654321],
            ...     'micrograph_blob/path': '/path/to/exposure.mrc'
            ... })

        """
        if isinstance(query, dict):
            return self.mask(self.query_mask(query))
        else:
            mask = [query(row) for row in self.rows()]
            return self.mask(mask)

    def query_mask(self, query: Dict[str, "nt.ArrayLike"], invert=False) -> "nt.NDArray[n.bool_]":
        """
        Get a boolean array representing the items to keep in the dataset that
        match the given query filter. See `query` method for example query
        format.
        """
        query_fields = set(self.fields()).intersection(query.keys())
        mask = n.ones(len(self), dtype=bool)
        for field in query_fields:
            mask &= n.isin(self[field], query[field])

        return n.invert(mask, out=mask) if invert else mask

    def subset(self, rows: Collection[Row]):
        """
        Get a subset of dataset that only includes the given list of rows (from
        this dataset)
        """
        return self.indexes([row.idx for row in rows])

    def indexes(self, indexes: Union[List[int], "nt.NDArray"]):
        return type(self)([(f, col[indexes]) for f, col in self.items()])

    def mask(self, mask: Union[List[bool], "nt.NDArray"]):
        """
        Get a subset of the dataset that matches the given mask of rows
        """
        assert len(mask) == len(self), f"Mask with size {len(mask)} does not match expected dataset size {len(self)}"
        return type(self)([(f, col[mask]) for f, col in self.items()])

    def slice(self, start: int = 0, stop: Optional[int] = None, step: int = 1):
        """
        Get at subset of the dataset with rows in the given range
        """
        return type(self)([(f, col[slice(start, stop, step)]) for f, col in self.items()])

    def split_by(self, field: str):
        """
        Create a mapping from possible values of the given field and to a
        datasets filtered by rows of that value.

        Examples:


            >>> dset = Dataset([
            ...     ('uid', [1, 2, 3, 4]),
            ...     ('foo', ['hello', 'world', 'hello', 'world'])
            ... ])
            >>> assert dset.split_by('foo') == {
            ...     'hello': Dataset([('uid', [1, 3]), ('foo', ['hello', 'hello'])]),
            ...     'world': Dataset([('uid', [2, 4]), ('foo', ['world', 'world'])])
            ... }

        """
        cols = self.cols()
        col = cols[field]
        idxs = {}
        for idx, val in enumerate(col):
            curr = idxs.get(val, [])
            curr.append(idx)
            idxs[val] = curr

        return {val: self.indexes(idx) for val, idx in idxs.items()}

    def replace(self, query: Dict[str, "nt.ArrayLike"], *others: "Dataset", assume_disjoint=False, assume_unique=False):
        """
        Replaces values matching the given query with others. The query is a
        key/value map of allowed field values. The values can be either a single
        scalar value or a set of possible values. If nothing matches the query
        (e.g., {} specified), works the same way as append.

        Specify `assume_disjoint=True` when all input datasets do not any UIDs
        in common.

        Specify `assume_unique=True` when all input datasets do not have any
        duplicate UIDs.
        """
        keep_fields = self.common_fields(self, *others, assert_same_fields=True)
        others_len = sum(len(o) for o in others)
        keep_mask = n.ones(len(self), dtype=bool)
        if not assume_disjoint:
            uids = self["uid"]
            for other in others:
                keep_mask &= n.isin(uids, other["uid"], assume_unique=assume_unique, invert=True)
        if query:
            keep_mask &= self.query_mask(query, invert=True)

        offset = keep_mask.sum()
        result = Dataset.allocate(offset + others_len, keep_fields)
        for key, col in self.items():
            result[key][:offset] = col[keep_mask]

        for other in others:
            other_len = len(other)
            for field, value in result.items():
                value[offset : offset + other_len] = other[field]
            offset += other_len

        return result

    def __repr__(self) -> str:
        s = f"{type(self).__name__}(["
        size = len(self)

        cols = self.cols()
        for k, v in cols.items():
            if size > 6:
                contents = f"{str(v[:3])[:-1]} ... {str(v[-3:])[1:]}"
            else:
                contents = str(v)
            contents = " ".join(contents.split("\n"))
            contents = " ".join([x for x in contents.split(" ") if len(x) > 0])
            s += "\n" + f"    ('{k}', {contents}),"

        s += f"\n])  # {size} items, {len(cols)} fields"
        return s

    def _ipython_key_completions_(self):
        return self.fields()


def generate_uids(num: int = 0):
    """
    Generate the given number of random 64-bit unsigned integer uids
    """
    return n.random.randint(0, 2**64, size=(num,), dtype=n.uint64)
