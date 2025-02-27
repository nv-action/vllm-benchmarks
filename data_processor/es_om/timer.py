from datetime import datetime, timedelta
import pytz

from handler import DataHandler
from db import get_es_schema


def get_datetime(day: int=0):
    """
    Get datetime of `day` days ago from today in ISO format.
    """
    shanghai_tz = pytz.timezone("Asia/Shanghai")
    now_shanghai = datetime.now(shanghai_tz)  
    days_ago = now_shanghai - timedelta(days=day)  
    return str.split(days_ago.isoformat(), "+")[0]


def update_all_schema_time(commit_id: str=None, created_at: str=None):
    schemas = get_es_schema()
    dataHandler = DataHandler()
    for schema in schemas:
        dataHandler.index_name = schema
        data_list = dataHandler.search_data_from_vllm(schema, source=True)['hits']['hits']
        print(f"in schema: {schema}")
        for data in data_list:
            _id = data['_id']
            source = data['_source']
            created_at = source.get('created_at', None)
            print(f"id:{_id}, created_at:{created_at}")
            if created_at:
                dataHandler.update_data_for_exist_id(_id, {"created_at": get_datetime()})


if __name__ == '__main__':
    update_all_schema_time()