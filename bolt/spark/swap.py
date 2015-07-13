from numpy import zeros, ones, asarray, r_, concatenate, arange, ceil, prod

from itertools import product

from bolt.utils import tuplesort


class ChunkedArray(object):
    """
    This class implements the underlying logic for swap operations (that is, 
    operations that move axes of an ndarray from being 'in the keys' to being
    'in the values'. It is initiated and called from swap() and chunk() methods
    within BoltArraySpark.

    The overaching idea with this implementation is that for every
    value-dimension that becomes a key, you slice the data along that
    dimension into 'chunks' of a user-specified size. This is
    implemented in an intermediate form that can be transformed back
    into a BoltSparkArray.

    This class implements the following methods:

    - getplan() - figure out how many chunks to break each value along the new key dimension
    - getslices() - actually calculate the slices needed to execute the plant
    - chunk() - take an RDD and chunk it according to desired keys and values
    - extract() - take a chunked RDD and transform it back to a BoltSparkArray
    - getshape() - returns the shape of a new swapped array
    """
    _metadata = ['_shape', '_split', '_dtype']

    def __init__(self, rdd, shape, split, dtype):
        self._rdd = rdd
        self._shape = shape
        self._split = split
        self._dtype = dtype

    @property
    def dtype(self):
        return self._dtype

    @property
    def key(self):
        return Dims(self._shape[:self._split])

    @property
    def value(self):
        return Dims(self._shape[self._split:])

    @property
    def _constructor(self):
        return ChunkedArray

    def __finalize__(self, other):
        for name in self._metadata:
            other_attr = getattr(other, name, None)
            if (other_attr is not None) and (getattr(self, name, None) is None):
                object.__setattr__(self, name, other_attr)
        return self

    def getshape(self, key_axes, value_axes):
        """
        Get resulting shape after swapping. This returns an array[int] of:
        [unswapped keys, swapped values, swapped keys, unswapped values]
        """
        return r_[self.key.shape[~self.key.mask(key_axes)], self.value.shape[self.value.mask(value_axes)],
                  self.key.shape[self.key.mask(key_axes)], self.value.shape[~self.value.mask(value_axes)]].astype('int')

    def chunk(self, rdd, size, key_axes, value_axes):
        """
        Convert values of a BoltSparkArray into chunks. This transforms
        the underlying pair RDD of (keys, values) into records of the
        form: (chunk #, stationary keys), (moving keys, chunked values).
        Here, Chunk #, stationary keys, moving keys are all tuples.
        Chunked data is a subset of the data in each value, that has
        been sliced along 'chunk' lines. That is, for each
        value-dimnesion that is going to become a key-dimension, you
        break the value (i.e. the data in a single record) into chunks
        along those dimensions.

        Thus, the data can be collected and reconstructed in extract()
        without having to pull all of it onto the driver program.

        Parameters
        ----------
        rdd : Bolt RDD 
            Must have compatible key, values, and dtype as the current object.
            Typically this is the underlying RDD of the BoltSparkArray 
            used to initiate the Swapper object.
        """
        key_axes, value_axes = asarray(key_axes, 'int'), asarray(value_axes, 'int')
        kmask, vmask = self.key.mask(key_axes), self.value.mask(value_axes)
        plan = self.getplan(size, key_axes, value_axes)
        slices, _ = self.getslices(plan, self.value.shape)

        labeled_slices = list(product(*[list(enumerate(s)) for s in slices]))
        scheme = [list(zip(*s)) for s in labeled_slices]

        # this helper function returns a new pair rdd
        # keys = (chunk #, non-swapped keys)
        # values = (swapped keys, chunked data)
        def _chunk(record):
            k, v = record[0], record[1]
            k = asarray(k)
            
            stationary = tuple(k[~kmask])
            moving = k[kmask]
            for (chk, slc) in scheme:
                k = (tuple(asarray(chk)[vmask]), stationary)
                yield k, (moving, v[slc])

        return rdd.flatMap(_chunk).groupByKey()

    def extract(self, rdd, size, key_axes, value_axes):
        """
        Convert values of a chunked BoltSparkArray back into a proper form to
        underly a BoltSparkArray i.e. (key, value), where key is a tuple of indicies,
        and value is an ndarray. Generally the input to this function will be an RDD
        from chunk().

        Parameters
        ----------
        rdd : pair RDD 
            Must have the form ((chunk #, stationary keys), 
            (moving keys, chunked values)). Chunk #, stationary keys, 
            moving keys are all tuples, and chunked values are ndarrays.
        """
        kmask, vmask = self.key.mask(key_axes), self.value.mask(value_axes)
        kshape, vshape = self.key.shape, self.value.shape
        plan = self.getplan(size, key_axes, value_axes)
        _, chunk_sizes = self.getslices(plan, self.value.shape)

        moving_key_shape = kshape[kmask]

        mask = [False for _ in moving_key_shape]
        mask.extend([True if vmask[k] else False for k in range(len(vmask))])
        mask = asarray(mask)

        slices = [slice(0, i, 1) for i in moving_key_shape]
        slices.extend([None if vmask[i] else slice(0, vshape[i], 1) for i in range(len(vmask))])
        slices = asarray(slices)

        def _extract(record):

            k, v = record[0], record[1]

            chunk, stationary_key = k[0], k[1]
            key_offsets = prod([asarray(chunk), asarray(chunk_sizes)[vmask]], axis=0)
            moving_keys, values = zip(*v.data)
            sorted_keys = tuplesort([i.tolist() for i in moving_keys])
            values_sorted = asarray(values)[sorted_keys]
            expanded_shape = concatenate([moving_key_shape, values_sorted.shape[1:]])
            bounds = asarray(values_sorted[0].shape)[vmask]
            indices = list(product(*map(lambda x: arange(x), bounds)))
            values = values_sorted.reshape(expanded_shape)

            for b in indices:
                s = slices.copy()
                s[mask] = b
                yield (tuple(asarray(r_[stationary_key, key_offsets + b], dtype='int')), values[tuple(s)])

        return rdd.flatMap(_extract)

    def getplan(self, size, key_axes, value_axes):
        """
        Identify the plan for chunking along each value-dimension. This
        generates an ndarray with the number of chunks in each
        dimension. Any dimension that is staying in the values is set
        as a single chunk.

        Typical size parameter is 150 (an int, megabytes)

        Parameters
        ----------
        size : integer or tuple
             If int, the average size of the chunks in all value dimensions.  
             If tuple, an explicit specification of the number chunks in 
             each moving value dimension.

        dtype : dtype 
              Valid dtype of the underlying data, used to calculate 
              size in each chunk.
        """
        from numpy import dtype as gettype
        plan = ones(len(self.value.shape), dtype=int)

        value_axes = asarray(value_axes, 'int')

        if isinstance(size, tuple):
            plan[value_axes] = size

        else:
            # convert from megabytes
            size *= 1000.0

            # calculate from dtype
            element_size = gettype(self.dtype).itemsize
            nelements = prod(self.value.shape)
            total_size = nelements * element_size
            moving_value_shapes = self.value.shape[self.value.mask(value_axes)]

            if size <= element_size:
                return moving_value_shapes

            remaining_size = 1.0*total_size
            nchunks = ones(len(moving_value_shapes))
            for (i, s) in enumerate(moving_value_shapes):
                min_chunk_size = remaining_size/s
                if min_chunk_size >= size:
                    nchunks[i] = s
                    remaining_size = min_chunk_size
                    continue
                else:
                    nchunks[i] = ceil(remaining_size/size)
                    break

            plan[value_axes] = nchunks

        return plan

    @staticmethod
    def getslices(plan, dims):
        """
        Obtain slices for the given dimensions and chunks. Given a plan for chunking
        each moving value dimension, calculate a list of slices required to generate chunks
        of that size.

        Parameters
        ----------
        plan : ndarray
             Length must be equal to the number of value dimensions; generated by
             getplan(). Each entry contains the number of chunks along that dimension.

        dims : tuple
             Shape of the new vaues
        """
        slices = []
        sizes = []
        for nchunks, d in zip(plan, dims):
            size = ceil(1.0*d/nchunks)
            sizes.append(size)
            chunk_remainder = d % nchunks
            start = 0
            dim_slices = []
            for idx in range(nchunks):
                end = start + size
                dim_slices.append(slice(start, end, 1))
                start = end
            if chunk_remainder:
                dim_slices.append(slice(end, d, 1))
            slices.append(dim_slices)
        return slices, sizes


class Dims(object):
    """
    Class for storing properties associated with dimensionality.
    Objects of this class are input arguments for Swapper, and 
    implement axes, shape, and mask (boolean array with True in the
    represented axes locations)
    """
    def __init__(self, shape):
        self.shape = asarray(shape)

    def mask(self, axes):
        """
        Return a boolean array which uses True to mark the arrays
        represented by this object.
        """
        axes = asarray(axes, 'int')
        mask = zeros(len(self.shape), dtype=bool)
        mask[axes] = True
        return mask
