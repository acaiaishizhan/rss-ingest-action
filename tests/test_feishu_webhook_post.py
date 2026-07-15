from feishu_client import send_feishu_webhook_post


def test_send_feishu_webhook_post_signature():
    assert callable(send_feishu_webhook_post)
