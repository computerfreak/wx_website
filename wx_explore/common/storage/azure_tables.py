from azure.cosmosdb.table.tableservice import TableService
from azure.cosmosdb.table import (
    Entity,
    EntityProperty,
    EdmType,
    TableBatch,
)
from typing import Dict, Tuple, List

import array
import concurrent.futures
import datetime
import dateutil.parser
import logging
import numpy
import zlib

from . import DataProvider
from wx_explore.common.models import (
    Projection,
    SourceField,
    DataPointSet,
)
from wx_explore.common.utils import chunk


class AzureTableBackend(DataProvider):
    logger: logging.Logger
    account_name: str
    account_key: str
    table_name: str

    def __init__(self, account_name, account_key, table_name):
        logging.getLogger('azure').setLevel(logging.WARNING)
        logging.getLogger('urllib3').setLevel(logging.ERROR)

        self.logger = logging.getLogger(self.__class__.__name__)
        self.account_name = account_name
        self.account_key = account_key
        self.table_name = table_name

    # entity size 1mb, up to 255 props
    # pk is (proj_id, y)
    # row valid,run
    # key
        # field -> bytearr value for each x (~4k each, max 64k)

    def get_fields(
            self,
            proj_id: int,
            loc: Tuple[float, float],
            valid_source_fields: List[SourceField],
            start: datetime.datetime,
            end: datetime.datetime
    ) -> List[DataPointSet]:
        start = start.replace(microsecond=0)
        end = end.replace(microsecond=0)

        times = [start]
        while times[-1] + datetime.timedelta(hours=1) < end:
            times.append(times[-1] + datetime.timedelta(hours=1))
        times.append(end)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(times)-1) as ex:
            return sum(
                ex.map(
                    lambda time_range: self._get_fields_worker(proj_id, loc, valid_source_fields, *time_range),
                    zip(times[:-1], times[1:]),
                ),
                [],
            )

    def _get_fields_worker(
            self,
            proj_id: int,
            loc: Tuple[float, float],
            valid_source_fields: List[SourceField],
            start: datetime.datetime,
            end: datetime.datetime
    ) -> List[DataPointSet]:
        x, y = loc
        partition = f"{proj_id}-{y}"

        # This actually (ab)uses lexicographical string compares on the rowkey
        row_start = start.isoformat()
        row_end = end.isoformat()

        az_filter = f"PartitionKey eq '{partition}' and RowKey gt '{row_start}' and RowKey lt '{row_end}'"
        select = ['PartitionKey', 'RowKey', *(f"f{sf.id}" for sf in valid_source_fields)]

        data_points = []

        for row in TableService(self.account_name, self.account_key).query_entities(self.table_name, az_filter, ','.join(select)):
            valid_time, run_time = map(dateutil.parser.parse, row.RowKey.split(','))

            for sf in valid_source_fields:
                key = f"f{sf.id}"
                if key not in row or row[key] is None:
                    continue

                raw = zlib.decompress(row[key].value)
                val = array.array("f", raw).tolist()[x]

                data_point = DataPointSet(
                    values=[val],
                    metric_id=sf.metric.id,
                    valid_time=valid_time,
                    source_field_id=sf.id,
                    run_time=run_time,
                )

                data_points.append(data_point)

        return data_points

    def put_fields(
            self,
            proj: Projection,
            fields: Dict[Tuple[int, datetime.datetime, datetime.datetime], List[numpy.array]]
    ):
        # fields is map of (field_id, valid_time, run_time) -> [msg, ...]
        with concurrent.futures.ThreadPoolExecutor() as ex:
            ex.map(lambda y: self._put_fields_worker(proj, fields, y), range(proj.n_y))

    def _put_fields_worker(self, proj: Projection, fields: Dict[Tuple[int, datetime.datetime, datetime.datetime], List[numpy.array]], y: int):
        partition = f"{proj.id}-{y}"
        rows = {}

        for (field_id, valid_time, run_time), msgs in fields.items():
            row_key = f"{valid_time.isoformat()},{run_time.isoformat()}"
            if row_key not in rows:
                rows[row_key] = {}

            for msg in msgs:
                # XXX: this only keeps last msg per field breaking ensembles
                rows[row_key][f"f{field_id}"] = EntityProperty(EdmType.BINARY, zlib.compress(msg[y].tobytes()))

        with TableService(self.account_name, self.account_key).batch(self.table_name) as batch:
            for row_key, row in rows.items():
                batch.insert_or_merge_entity({
                    'PartitionKey': partition,
                    'RowKey': row_key,
                    **row,
                })

    def clean(self, oldest_time: datetime.datetime):
        earliest = oldest_time.replace(microsecond=0)

        for proj in Projection.query.all():
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
                ex.map(lambda y: self._clean_worker(earliest, proj, y), range(proj.n_y))

    def _clean_worker(self, earliest: datetime.datetime, proj: Projection, y: int):
        svc = TableService(self.account_name, self.account_key)
        to_delete = []

        for row in svc.query_entities(self.table_name, f"PartitionKey eq '{proj.id}-{y}' and RowKey lt '{earliest.isoformat()}'", 'PartitionKey,RowKey'):
            to_delete.append((row.PartitionKey, row.RowKey))

        for batch_elems in chunk(to_delete, 100):
            with svc.batch(self.table_name) as batch:
                for entity in batch_elems:
                    batch.delete_entity(*entity)

    def merge(self):
        pass