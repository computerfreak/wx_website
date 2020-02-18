from scipy.spatial import cKDTree
from typing import List, Dict, Tuple
import binascii
import datetime
import logging
import numpy
import pickle
import pygrib
import random

from wx_explore.common.models import (
    Projection,
    FileMeta,
    FileBandMeta,
)
from wx_explore.common.queue import pq
from wx_explore.common.storage import get_s3_bucket
from wx_explore.ingest.sources.source import IngestSource
from wx_explore.web.core import db

logger = logging.getLogger(__name__)


def get_queue():
    return pq['ingest']


def get_or_create_projection(msg):
    lats, lons = msg.latlons()

    # GFS (and maybe others) have lons that range 0-360 instead of -180 to 180.
    # If found, transform them to match the standard range.
    if lons.max() > 180:
        lons = numpy.vectorize(lambda n: n if 0 <= n < 180 else n-360)(lons)

    ll_hash = binascii.crc32(numpy.array([lats, lons]).tobytes())

    projection = Projection.query.filter_by(
        params=msg.projparams,
        ll_hash=ll_hash,
    ).first()

    if projection is None:
        logger.info("Creating new projection with params %s", msg.projparams)
        tree = cKDTree(numpy.stack([lons.ravel(), lats.ravel()], axis=-1))

        projection = Projection(
            params=msg.projparams,
            n_x=msg.values.shape[1],
            n_y=msg.values.shape[0],
            ll_hash=ll_hash,
            lats=lats.tolist(),
            lons=lons.tolist(),
            tree=pickle.dumps(tree),
        )
        db.session.add(projection)
        db.session.commit()

    return projection


def create_files(proj_id: int, fields: Dict[Tuple[int, datetime.datetime, datetime.datetime], List[numpy.array]]):
    metas = []
    vals = []

    s3_file_name = ''.join(random.choices('0123456789abcdef', k=32))
    s3_file_name = s3_file_name[:2] + '/' + s3_file_name

    fm = FileMeta(
        file_name=s3_file_name,
        projection_id=proj_id,
    )
    db.session.add(fm)
    db.session.commit()

    offset = 0
    for i, ((field_id, valid_time, run_time), msgs) in enumerate(fields.items()):
        metas.append(FileBandMeta(
            file_name=s3_file_name,
            source_field_id=field_id,
            valid_time=valid_time,
            run_time=run_time,
            offset=offset,
            vals_per_loc=len(msgs),
        ))

        for msg in msgs:
            vals.append(msg.astype(numpy.float32))
            offset += 4  # sizeof(float32)

    fm.loc_size = offset

    s3 = get_s3_bucket()
    s3.put_object(
        Key=s3_file_name,
        Body=numpy.stack(vals, axis=-1).tobytes(),
    )

    db.session.add_all(metas)
    db.session.commit()


def get_source_modules():
    from wx_explore.ingest.sources.hrrr import HRRR
    from wx_explore.ingest.sources.gfs import GFS
    from wx_explore.ingest.sources.nam import NAM

    return {
        c.SOURCE_NAME: c for c in (HRRR, GFS, NAM)
    }


def get_source_module(short_name: str) -> IngestSource:
    return get_source_modules()[short_name]