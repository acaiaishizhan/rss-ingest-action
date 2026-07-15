from feishu_client import send_feishu_webhook


def test_send_feishu_webhook_signature():
    assert callable(send_feishu_webhook)
