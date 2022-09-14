import argparse
import base64
import json
import socket
from time import sleep
import yaml
import requests
# from fybrik_python_vault import get_jwt_from_file, get_raw_secret_from_vault
from fybrik_python_logging import init_logger, logger, DataSetID, ForUser, Error

data_dict = {}

#########################
def get_jwt_from_file(file_name):
    """
    Getting a jwt from a file.
    Typically, an SA token, which would be at: /var/run/secrets/kubernetes.io/serviceaccount/token
    """
    with open(file_name) as f:
        return f.read()

def vault_jwt_auth(jwt, vault_address, vault_path, role, datasetID):
    """Authenticate against Vault using a JWT token (i.e., k8s sa token)"""
    full_auth_path = vault_address + vault_path
    logger.trace('authenticating against vault using a JWT token',
        extra={'full_auth_path': str(full_auth_path),
               DataSetID: datasetID})
    json = {"jwt": jwt, "role": role}
    response = requests.post(full_auth_path, json=json)
    if response.status_code == 200:
        return response.json()
    logger.error("vault authentication failed",
        extra={Error: str(response.status_code) + ': ' + str(response.json()),
               DataSetID: datasetID, ForUser: True})
    return None

def get_raw_secret_from_vault(jwt, secret_path, vault_address, vault_path, role, datasetID):
    """Get a raw secret from vault by providing a valid jwt token"""
    vault_auth_response = vault_jwt_auth(jwt, vault_address, vault_path, role, datasetID)
    if vault_auth_response is None:
        logger.error("Empty vault authorization response",
                     extra={DataSetID: datasetID, ForUser: True})
        return None
    if not "auth" in vault_auth_response or not "client_token" in vault_auth_response["auth"]:
        logger.error("Malformed vault authorization response",
                     extra={DataSetID: datasetID, ForUser: True})
        return None
    client_token = vault_auth_response["auth"]["client_token"]
    secret_full_path = vault_address + secret_path
    response = requests.get(secret_full_path, headers={"X-Vault-Token" : client_token})
    logger.debug('Response received from vault when accessing credentials: %s', str(response.status_code),
        extra={'credentials_path': str(secret_full_path),
               DataSetID: datasetID, ForUser: True})
    if response.status_code == 200:
        response_json = response.json()
        if 'data' in response_json:
            return response_json['data']
        else:
            logger.error("Malformed secret response. Expected the 'data' field in JSON",
                         extra={DataSetID: datasetID, ForUser: True})
    else:
        logger.error("Error reading credentials from vault",
            extra={Error: str(response.status_code) + ': ' + str(response.json()),
                   DataSetID: datasetID, ForUser: True})
    return None
#########################   

def get_credentials_from_vault(vault_credentials, data_set_id):
    """ Use the fybrik-python-library to get the access_key and secret_key from vault for s3 dataset """
    jwt_file_path = vault_credentials.get('jwt_file_path', '/var/run/secrets/kubernetes.io/serviceaccount/token')
    jwt = get_jwt_from_file(jwt_file_path)
    vault_address = vault_credentials.get('address', 'https://localhost:8200')
    secret_path = vault_credentials.get('secretPath', '/v1/secret/data/cred')
    vault_auth = vault_credentials.get('authPath', '/v1/auth/kubernetes/login')
    role = vault_credentials.get('role', 'demo')
    credentials = get_raw_secret_from_vault(jwt, secret_path, vault_address, vault_auth, role, data_set_id)
    if not credentials:
        raise ValueError("Vault credentials are missing")
    if 'access_key' in credentials and 'secret_key' in credentials:
        if credentials['access_key'] and credentials['secret_key']:
            return credentials['access_key'], credentials['secret_key']
        if not credentials['access_key']:
            logger.error("'access_key' must be non-empty",
                            extra={DataSetID: data_set_id, ForUser: True})
        if not credentials['secret_key']:
            logger.error("'secret_key' must be non-empty",
                            extra={DataSetID: data_set_id, ForUser: True})
    logger.error("Expected both 'access_key' and 'secret_key' fields in vault secret",
                 extra={DataSetID: data_set_id, ForUser: True})
    raise ValueError("Vault credentials are missing")

def get_details_from_conf():
    """ Parse the configuration and get the data details and policies """
    with open("/etc/conf/conf.yaml", 'r') as stream:
        content = yaml.safe_load(stream)
        for key, val in content.items():
            if "data" in key:
                for data in val:
                    dataset_id = data["name"]
                    name = dataset_id.split("/")[1]
                    endpoint_url = data["connection"]["s3"]["endpoint_url"]
                    vault_credentials = data["connection"]["s3"]["vault_credentials"]
                    creds = get_credentials_from_vault(vault_credentials, dataset_id)
                    transformations = base64.b64decode(data["transformations"])
                    transformations_json = json.loads(transformations.decode('utf-8'))
                    transformation = transformations_json[0]['name']
                    transformation_cols = transformations_json[0][transformation]["columns"]
                    data_dict[name] = {'format': data["format"], 'endpoint_url': endpoint_url, 'path': data["path"], 'transformation': transformation,
                     'transformation_cols': transformation_cols, 'creds': creds}
    return data_dict[name]

def api_get(server, endpoint=None, headers=None, body=None):
    """" Run GET command """
    return json.loads(requests.get(url='{server}/api/v3/{endpoint}'.format(server=server, endpoint=endpoint), headers=headers, data=json.dumps(body)).text)

def api_post(server, endpoint=None, body=None, headers=None):
    """" Run POST command """
    text = requests.request("POST", '{server}/api/v3/{endpoint}'.format(server=server, endpoint=endpoint), headers=headers, data=json.dumps(body)).text

    # a post may return no data
    if (text):
        return json.loads(text)
    else:
        return None

def login(server, username, password, headers=None):
    """" Login to Dremio using the given username and password """
    loginData = {'userName': username, 'password': password}
    response = requests.post('{server}/apiv2/login'.format(server=server), headers=headers, data=json.dumps(loginData))
    logger.info("Login response: %s", response)
    data = json.loads(response.text)

    # retrieve the login token
    token = data['token']
    return {'Content-Type': 'application/json', 'Authorization': '_dremio{authToken}'.format(authToken=token)}

def wait_dremio(dremio_host, dremio_port):
    """ Try to connect to Dremio until success or timeout """
    a_socket = socket.socket()
    count = 0
    while count < 30:
        logger.info("wait dremio")
        try:
            a_socket.connect((dremio_host, dremio_port))
        except:
            logger.info("sleeping, waiting dremio")
            sleep(10)
            count += 1
            continue
        break

def register_admin_user(dremio_server):
    data_user = {
        "userName": "adminUser",
        "firstName": "user",
        "lastName": "admin",
        "email": "test@test.com",
        "createdAt": 1526186430755,
        "password": "adminPwd1",
    }
    headers = {'Content-Type': 'application/json', 'Authorization': '_dremionull'}
    response = requests.request("PUT", dremio_server + '/apiv2/bootstrap/firstuser', data=json.dumps(data_user), headers=headers)
    logger.info("register user response: %s", response.text)

def create_s3_source(dremio_server, auth_headers, creds, endpoint, source_name):
    data_s3 = {
        "entityType": "source",
        "name": source_name,
        "type": "S3",
        "config": {
            "accessKey": creds[0],
            "accessSecret": creds[1],
            "secure": "false",
            "allowCreateDrop": "true",
            "rootPath": "/",
            "credentialType": "ACCESS_KEY",
            "enableAsync": "true",
            "compatibilityMode": "true",
            "isCachingEnabled": "true",
            "maxCacheSpacePct": 100,
            "requesterPays": "false",
            "enableFileStatusCheck": "true",
            "propertyList": [
                {"name": "fs.s3a.path.style.access", "value": "true"},
                {"name": "fs.s3a.endpoint", "value": endpoint},
            ],
        },
    }
    response = api_post(dremio_server, "catalog", data_s3, auth_headers)
    logger.debug("new source response: %s", response)

def promote_folder(dremio_server, auth_headers, path, source_name):
    path_list = path.split('/')
    path_list = [source_name] + path_list
    dataPromote = {
        "entityType": "dataset",
        "id": "dremio:/" + source_name + "/" + path,
        "path": path_list,
        "type": "PHYSICAL_DATASET",
        "format": {
            "type": "Iceberg"
        }
    }
    promote_url = source_name + '%2F' + '%2F'.join(path_list[1:])
    response = api_post(dremio_server, "catalog/dremio%3A%2F" + promote_url, dataPromote, auth_headers)
    logger.debug("promote response: %s", response)
    return path_list

def get_table_columns(dremio_server, auth_headers, sql_path):
    dataSQL = {
        "sql": 'SELECT * FROM "' + sql_path + 'LIMIT 0'
    }
    response = api_post(dremio_server, "sql", dataSQL, auth_headers)
    job_id = response.get("id")
    
    response = api_get(dremio_server, "job/"+job_id, auth_headers, dataSQL)
    while(response.get("jobState") != "COMPLETED"):
        logger.info("wait for job")
        response = api_get(dremio_server, "job/"+job_id, auth_headers, dataSQL)
        sleep(10)
    response = api_get(dremio_server, "job/"+job_id+"/results", auth_headers, dataSQL)
    col_names = [elem.get("name") for elem in response.get("schema")]
    logger.debug("Table's columns: %s", col_names)
    return col_names

def get_policy_query(transformation_cols, sql_path, col_names):
    request_cols = [col for col in col_names if col not in transformation_cols]
    if len(request_cols) < 1:
        logger.debug("empty dataset")
        return ""
    requested_cols_string = request_cols[0]
    for col in request_cols[1:]:
        add_col = ", " + col
        requested_cols_string += add_col
    sql_vds = "select " + requested_cols_string + ' from "' + sql_path
    logger.debug("SQL to build VDS: %s", sql_vds)
    return sql_vds

def create_VDS(dremio_server, auth_headers, path_list, sql_vds, newVDSName):
    dataVDS = {
        "entityType": "dataset",
        "path": [
            "Space-api",
            newVDSName,
        ],
	    "type": "VIRTUAL_DATASET",
	    "sql": sql_vds,
	    "sqlContext": path_list
    }
    response = api_post(dremio_server, "catalog", dataVDS, auth_headers)
    logger.info("Create VDS response: %s", response)

def create_new_user(dremio_server, auth_headers):
    dataNewUser = {
        "name": "newUser",
        "firstName": "first",
        "password": "testpassword123",
    }
    response = api_post(dremio_server, "user", dataNewUser, auth_headers)
    logger.debug("Create new user response: %s", response)

if __name__ == "__main__":
    init_logger("TRACE", "123", 'dremio-module')
    username = "adminUser"
    password = "adminPwd1"
    json_headers = {'content-type': 'application/json'}
    dremio_server = 'http://dremio-client.fybrik-blueprints.svc.cluster.local:9047'
    dremio_host = 'dremio-client.fybrik-blueprints.svc.cluster.local'
    dremio_port = 9047

    # Wait for dremio to be ready
    wait_dremio(dremio_host, dremio_port)
    # Register the admin user
    register_admin_user(dremio_server)
    # Login to Dremio with admin user
    auth_headers = login(dremio_server, username, password, json_headers)

    # Get the dataset details from configuration
    parse_conf = get_details_from_conf()
    transformation = parse_conf['transformation']
    transformation_cols = parse_conf['transformation_cols']
    creds = parse_conf['creds']
    endpoint = parse_conf['endpoint_url']
    path = parse_conf['path']
    if "://" in endpoint:
        endpoint = endpoint.split("://")[1]

    # Create a new source from an s3 bucket
    source_name = "sample-iceberg"
    logger.info("Creating S3 resource")
    create_s3_source(dremio_server, auth_headers, creds, endpoint, source_name)

    # Get data folder path
    response = api_get(dremio_server, endpoint="catalog/by-path/{name}/{path}".format(name=source_name, path=path), headers=auth_headers)
    logger.info("Get path of the data folder: %s", response)

    # Promote a folder to dataset
    path_list = promote_folder(dremio_server, auth_headers, path, source_name)

    # Get the columns of the new source
    sql_path = source_name + '"."' + '"."'.join(path_list[1:]) + '"'
    col_names = get_table_columns(dremio_server, auth_headers, sql_path)

    # Get the sql query from the policies
    sql_vds = get_policy_query(transformation_cols, sql_path, col_names)

    payloadSpace = {
        "entityType": "space",
        "name": "Space-api"
    }
    response = api_post(dremio_server, "catalog", payloadSpace, auth_headers)
    logger.info("Created space: %s", response)
    
    # Create a virtual dataset that represents the source dataset after applying the policies    
    newVDSName = "sample-iceberg-vds"
    logger.info("Creating VDS")
    create_VDS(dremio_server, auth_headers, path_list, sql_vds, newVDSName)

    # Add a new user
    create_new_user(dremio_server, auth_headers)
    logger.info("Finished!")
