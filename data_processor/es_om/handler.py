import os
import requests
import json
import logging


logger = logging.getLogger()

os.environ['ES_OM_DOMAIN'] = 'https://190.92.234.172:9200'
os.environ['ES_OM_AUTHORIZATION'] = 'Basic YWRtaW46bkJLWkU3Q2NLRk5oNGdRUExCZEA'
ES_OM_DOMAIN = os.getenv("ES_OM_DOMAIN")
ES_OM_AUTHORIZATION = os.getenv("ES_OM_AUTHORIZATION")

print(ES_OM_DOMAIN)
print(ES_OM_AUTHORIZATION)

class DataHandler():
    def __init__(self):
        self.headers = {
            "Content-Type": "application/x-ndjson",
            "Authorization": ES_OM_AUTHORIZATION
        }
        self.domain = ES_OM_DOMAIN
        self._index_name = "vllm_benchmarks"

    @property
    def index_name(self):
        return self._index_name


    @index_name.setter
    def index_name(self, value: str):
        self._index_name = value


    def create_table_with_property_type(self, property_type: dict):
        try:
            data = {
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 1
                },
                "mappings": {
                    "properties": property_type
                }
            }
            url = f"{self.domain}/{self._index_name}"
            resp = requests.put(
                url=url,
                headers=self.headers,
                data=json.dumps(data),
                verify=False
            )
            print(json.loads(resp.text))
        except Exception as e:
            logger.info(f"create table with property type error:\n{e}")


    def get_table_property_type(self):
        try:
            url = f"{self.domain}/{self._index_name}/_mapping"
            resp = requests.get(
                    url=url,
                    headers=self.headers,
                    verify=False
                )
            print(json.loads(resp.text))
        except Exception as e:
            logger.info(f"get table property type error:\n{e}")


    def delete_index(self, index_name):
        try:
            url = f"{self.domain}/{index_name}"
            resp = requests.delete(
                    url=url,
                    headers=self.headers,
                    verify=False
                )
            print(json.loads(resp.text))
        except Exception as e:
            logger.info(f"delete table[{index_name}] error:\n{e}")


    def reindex(self, source_idx, dest_idx):
        data = {
            "source": {
                "index": source_idx
            },
            "dest": {
                "index": dest_idx
            }
        }
        url = f"{self.domain}/{self._index_name}"
        resp = requests.put(
                url=url,
                headers=self.headers,
                data=json.dumps(data),
                verify=False
            )
        print(json.loads(resp.text))


    def _format_query_items(self, query_items: dict):
        query_bool = {"bool": {}}
        must_list = list()
        if query_items:
            for k, v in query_items.items():
                must_list.append(
                    {
                        "match_phrase": {
                            k : {"query": v}
                        }
                    }
                )
        if must_list:
            query_bool["bool"].update({"must": must_list})
        return query_bool


    def _query_scroll_search(self, query_items=None, query_range=None):
        url = f"{self.domain}/{self._index_name}/_search?scroll=1m"
        data = {
            "query": {"match_all": {}},
            "size": 5000,
            # "sort": [
            #     {"Date": {"order": "desc"}},
            # ]
        }
        if query_items:
            query_bool = self._format_query_items(query_items)
            data["query"] = query_bool
        if query_range:
            data["query"].update(query_range)

        import pdb;pdb.set_trace()
        data = {
            'query': {
                'bool': {'must': [
                    {'match_phrase': {'file_project': {'query': 'vllm-project/vllm'}}},
                    {"range": {"Date": {"gte": "2024-12-01", "lte": "2025-01-01"}}}
                ]}
            },
            'size': 5000
        }

        try:
            resp = requests.get(
                url=url,
                headers=self.headers,
                data=json.dumps(data),
                verify=False
            )
            res_data = json.loads(resp.text)
            data_list = res_data["hits"]["hits"]
            scroll_id = res_data["_scroll_id"]
            scroll_list = ["no use"]  # while condition
            cnt = 0
            while scroll_id and len(scroll_list) != 0:
                cnt += 1
                logger.info(f"query {self._index_name} scroll num[{cnt}]")
                data = {
                    "scroll": "1m",
                    "scroll_id": scroll_id
                }
                resp = requests.get(
                    url=f"{self.domain}/_search/scroll",
                    headers=self.headers,
                    data=json.dumps(data),
                    verify=False
                )
                res_data = json.loads(resp.text)
                if not res_data["hits"]:
                    continue
                scroll_list = res_data["hits"]["hits"]
                scroll_id = res_data["_scroll_id"]
                data_list += scroll_list
        except Exception as e:
            raise Exception(f"{e}")
        return data_list


    def _qr_remove_create(self, source_list: list, create_show):
        try:
            source_list.remove(self.create_at)
        except:
            pass
        if create_show:
            source_list += [self.create_at]
        return source_list


    def _qr_get_table_field(self, field_names, create_show):
        res: list = list()
        if not self.display_fields:
            res = self._qr_remove_create(field_names, create_show)
        else:
            self.display_fields = self._qr_remove_create(self.display_fields, create_show)
            # 根据self.display_fields剔除冗余项
            display_name: list = list()
            for name in field_names:
                if name in self.display_fields:
                    display_name.append(name)
            res = display_name

        return ["ID"] + res if create_show else res


    def _qr_get_record_order(self, field_names: list):
        try:
            res: list = list()
            for ele in self.field_order:
                try:
                    idx = field_names.index(ele)
                    res.append(idx)
                except:
                    pass
            return lambda x: tuple(x[i] for i in res)
        except Exception as e:
            raise Exception(f"record order process error {e}")


    def query_record(self, query_items=None, query_range=None, log_table=True, create_show=False):
        if not query_items:
            query_items = dict()

        data_list = self._query_scroll_search(query_items, query_range)
        if len(data_list) == 0:
            return None
        try:
            field_names = list(data_list[0]["_source"].keys())

            table = prettytable.PrettyTable()
            # table.field_names = self._qr_get_table_field(["ID"] + field_names, create_show)
            table.field_names = self._qr_get_table_field(field_names, create_show)
            all_record = []
            for ele in data_list:
                source_data = ele["_source"]
                # value_list = [ele["_id"]]  # 不要id了
                value_list = [ele["_id"]] if create_show else []
                for key in field_names:
                    if self.display_fields and key not in self.display_fields:
                        continue
                    if not create_show and key.startswith("create"):
                        continue
                    value_list.append(source_data.get(key, ""))

                all_record.append(value_list)

            # 排个序好对比
            if  self.field_order:
                all_record = sorted(
                    all_record,
                    key=self._qr_get_record_order(list(table.field_names))
                )
            for ele in all_record:
                table.add_row(ele)

            if log_table:
                logger.info(f"qury table record:\n{table}")
            return table
        except Exception as e:
            raise Exception(e)

    def search_data_from_vllm(self, _index: str, source: bool=False, size: int=1000):
        url = f'{self.domain}/{_index}/_search'
        data = {
            "_source": source,
            "size": 1000,
            "query": {
            "match_all": {}
                },
            "sort": [
            { "created_at": { "order": "desc" } } 
        ]
        }
        resp =  requests.post(
            url=url,
            headers=self.headers,
            json=data,
            verify=False
        )
        return resp.json()

    def _format_data_for_bulk_insert(self, data_list):
        """
        data_list: [[data_id1, item1],[data_id2, item2]]
                   item = {"file_project": ...,
                           "Type": ...,}
        """
        if not data_list:
            return

        actions = ""
        for data in data_list:
            item = data[1]
            index_data = {
                "index": {
                    "_index": self._index_name,
                    "_id": data[0]
                }
            }
            actions += json.dumps(index_data) + "\n"
            actions += json.dumps(item) + "\n"
        return actions

    def add_single_data(self, id: str, data: dict):
        url = f'{self.domain}/{self.index_name}/_doc/{id}'
        header = self.headers.copy()
        header['Content-Type'] = 'application/json'
        try:
            resp = requests.put(
                url=url,
                headers=header,
                json=data,
                verify=False
            )
            resp.raise_for_status()
            logger.info(f'add data to {self.index_name}/{id} successful, Response: {resp.json()}')
        except requests.exceptions.RequestException as req_err:
            logger.error(f'failed to add data {req_err}', exc_info=True)
        except Exception as other_err:
            logger.error(f'failed to add data {other_err}', exc_info=True)
        
    def query_today(self, index_name):
        query = {
            "query": {
                "range": {
                    "created_at": {
                        "gte": "now-5d/d",
                        "lt": "now/d",
                    }
                }
            }
        }
        url = f'{self.domain}/{index_name}/_search'
        header = self.headers.copy()
        header['Content-Type'] = 'application/json'
        try:
            resp = requests.post(url, headers=header, json=query, verify=False)
            resp.raise_for_status()
            logger.info(f'search  successful')
            return resp.json()
        except requests.exceptions.RequestException as req_err:
            logger.error(f"Request error during search: {req_err}", exc_info=True)

        except UnicodeEncodeError:
            logger.error("UnicodeEncodeError occurred while encoding data JSON", exc_info=True)
    
        except Exception as other_err:
            logger.error(f"Unexpected error during single search: {other_err}", exc_info=True)

    def condition_query(self, index_name, conditions):
        """
        查询指定索引下满足条件的所有 `_id`
        :param index_name: Elasticsearch 索引名
        :param conditions: 字段匹配条件（字典格式）
                           - `terms`: { "field_name": [value1, value2, ...] }  多个值匹配
                           - `range`: { "field_name": { "gte": value1, "lt": value2 } }  范围匹配
        :return: 匹配的 `_id` 列表
        """
        query = {"query": {"bool": {"must": []}}}

        for field, condition in conditions.items():
            if isinstance(condition, dict):  # 处理 range 查询
                query["query"]["bool"]["must"].append({"range": {field: condition}})
            elif isinstance(condition, list):  # 处理 terms 查询
                query["query"]["bool"]["must"].append({"terms": {field: condition}})
            else:  # 处理 match 查询（单个值）
                query["query"]["bool"]["must"].append({"match": {field: condition}})

        url = f"{self.domain}/{index_name}/_search"
        header = self.headers.copy()
        header['Content-Type'] = 'application/json'
        payload = {"_source": False, "size": 10000, "query": query} # 只获取 _id，最多 10000 条
        try:
            resp = requests.post(url, headers=header, data=payload, verify=False)
            resp.raise_for_status()
            hits = resp.json().get("hits", {}).get("hits", [])
            return [hit["_id"] for hit in hits]
        except requests.exceptions.RequestException as e:
            print(f"Error: {e}")
            return []


    def update_data_for_exist_id(self, id: str, data: dict):
        url = f'{self.domain}/{self.index_name}/_update/{id}'
        header = self.headers.copy()
        header['Content-Type'] = 'application/json'
        update_data = {'doc':data}
        try:
            resp = requests.post(
                url, 
                headers=header, 
                json=update_data, 
                verify=False,
                )
            resp.raise_for_status()
            logger.info(f'update data {self.index_name}/{id} successful')
        except requests.exceptions.RequestException as req_err:
            logger.error(f"Request error during update: {req_err}", exc_info=True)

        except UnicodeEncodeError:
            logger.error("UnicodeEncodeError occurred while encoding data JSON", exc_info=True)
    
        except Exception as other_err:
            logger.error(f"Unexpected error during single update: {other_err}", exc_info=True)

    def _bulk_insert(self, data_list: list):
        if not data_list:
            return
        interval = 1000
        n = int(len(data_list) / interval)
        if n <= 1:
            bulk_json = self._format_data_for_bulk_insert(data_list)
            self._put_bulk(bulk_json)
            return
        else:
            for i in range(n + 1):
                sub_list = data_list[i*interval:(i + 1)*interval]
                bulk_json = self._format_data_for_bulk_insert(sub_list)
                self._put_bulk(bulk_json)

    def _put_bulk(self, bulk_json):
        if not bulk_json or bulk_json == "":
            return

        # logger.info(f"start insert data:\n{bulk_json.strip()}")
        try:
            resp = requests.post(
                self.domain + "/_bulk",
                data=bulk_json.encode("utf-8"),
                headers=self.headers,
                verify=False
            )
            logger.info(f"finish insert data:\n{resp.text}\n")
        except UnicodeEncodeError:
            bulk_json = bulk_json.encode("iso-8859-1", "ignore")
            resp = requests.put(
                url=self.domain,
                data=bulk_json,
                headers=self.headers
            )
            logger.info(f"UnicodeEncode finish insert data:\n{resp.text}")
        except Exception as otherError:
            logger.info(f"Insert Exception:\n{otherError}")

    def _format_bulk_delete(self, id_lst):
        actions = ""
        for id in id_lst:
            index_data = {
                "delete": {
                    "_index": self._index_name,
                    "_id": id
                }
            }
            actions += json.dumps(index_data) + "\n"
        return actions

    def delete_id_list_with_bulk_insert(self, id_lst: list):
        if not id_lst:
            return

        bulk_json = self._format_bulk_delete(id_lst)
        self._put_bulk(bulk_json)

    def delete_with_item(self, query_items: dict):
        self._delete_by_query(query_items=query_items)

    def delete_all_record(self):
        self._delete_by_query(all_clear=True)
