import base64
from Crypto.Cipher import AES
# from boto3.dynamodb.conditions import Key
# from botocore.exceptions import ClientError
# import boto3

# dynamodb = boto3.resource("dynamodb")
# table = dynamodb.Table("digilux_device_data")  


def decrypt_mac(encoded_payload: str, secret_key: bytes) -> str:
    try:
        decoded_bytes = base64.b64decode(encoded_payload)
        nonce = decoded_bytes[:12]
        tag = decoded_bytes[12:28]
        ciphertext = decoded_bytes[28:]

        cipher = AES.new(secret_key, AES.MODE_GCM, nonce=nonce)
        decrypted_bytes = cipher.decrypt_and_verify(ciphertext, tag)
        return decrypted_bytes.decode('utf-8')
    except Exception as e:
        raise ValueError(f"Decryption failed: {str(e)}")

# def get_all_items_by_mac(mac_address):
#     items = []
#     last_evaluated_key = None

#     try:
#         while True:
#             query_params = {
#                 "IndexName": "macAddress-index",
#                 "KeyConditionExpression": Key("macAddress").eq(mac_address)
#             }

#             if last_evaluated_key:
#                 query_params["ExclusiveStartKey"] = last_evaluated_key

#             response = table.query(**query_params)
#             items.extend(response.get("Items", []))

#             last_evaluated_key = response.get("LastEvaluatedKey")
#             if not last_evaluated_key:
#                 break

#         return items

#     except ClientError as e:
#         logger.error(f"Pagination query failed: {e}")
#         raise

# def extract_device_id(items):
#     if not items:
#         return None

#     return items[0].get("deviceId")