from __future__ import annotations

import json
import math

from app.settings import (
    ALIYUN_ACCESS_KEY_ID,
    ALIYUN_ACCESS_KEY_SECRET,
    ALIYUN_SMS_VERIFY_ENDPOINT,
    ALIYUN_SMS_VERIFY_SCHEME_NAME,
    ALIYUN_SMS_VERIFY_SIGN_NAME,
    ALIYUN_SMS_VERIFY_TEMPLATE_CODE,
)


class SmsVerificationProviderError(RuntimeError):
    """Raised when the external SMS verification provider cannot serve a request."""


def _provider_error_message(exc: Exception, default: str) -> str:
    code = getattr(exc, "code", "") or ""
    data = getattr(exc, "data", None) or {}
    message = ""
    if isinstance(data, dict):
        message = str(data.get("Message") or data.get("message") or "")
        code = code or str(data.get("Code") or data.get("code") or "")
    message = message or str(getattr(exc, "message", "") or "")
    if code:
        return f"{default}：{code}{('，' + message) if message else ''}"
    return default


def _client():
    if not ALIYUN_ACCESS_KEY_ID or not ALIYUN_ACCESS_KEY_SECRET:
        raise SmsVerificationProviderError("短信服务尚未配置 AccessKey")
    if not ALIYUN_SMS_VERIFY_SIGN_NAME or not ALIYUN_SMS_VERIFY_TEMPLATE_CODE:
        raise SmsVerificationProviderError("短信服务尚未配置签名或模板")
    try:
        from alibabacloud_dypnsapi20170525.client import Client as DypnsapiClient
        from alibabacloud_tea_openapi import models as open_api_models
    except ModuleNotFoundError as exc:
        raise SmsVerificationProviderError("短信服务依赖尚未安装") from exc

    return DypnsapiClient(
        open_api_models.Config(
            access_key_id=ALIYUN_ACCESS_KEY_ID,
            access_key_secret=ALIYUN_ACCESS_KEY_SECRET,
            endpoint=ALIYUN_SMS_VERIFY_ENDPOINT,
        )
    )


def send_code(phone: str, *, code_length: int, valid_time: int, interval: int) -> dict:
    """Send a provider-generated verification code without exposing it to this app."""
    try:
        from alibabacloud_dypnsapi20170525 import models as dypnsapi_models

        request = dypnsapi_models.SendSmsVerifyCodeRequest(
            phone_number=phone,
            sign_name=ALIYUN_SMS_VERIFY_SIGN_NAME,
            template_code=ALIYUN_SMS_VERIFY_TEMPLATE_CODE,
            template_param=json.dumps(
                {"code": "##code##", "min": str(max(1, math.ceil(valid_time / 60)))},
                ensure_ascii=False,
            ),
            scheme_name=ALIYUN_SMS_VERIFY_SCHEME_NAME or None,
            code_length=code_length,
            code_type=1,
            valid_time=valid_time,
            interval=interval,
            duplicate_policy=1,
            return_verify_code=False,
        )
        response = _client().send_sms_verify_code(request)
    except SmsVerificationProviderError:
        raise
    except Exception as exc:
        raise SmsVerificationProviderError(_provider_error_message(exc, "短信发送失败")) from exc

    body = response.body
    if not body or body.code != "OK" or not body.success:
        message = getattr(body, "message", "") if body else ""
        code = getattr(body, "code", "") if body else ""
        detail = f"：{code}{('，' + message) if message else ''}" if code else ""
        raise SmsVerificationProviderError(f"短信发送失败{detail}")
    model = body.model
    return {
        "request_id": body.request_id or "",
        "biz_id": getattr(model, "biz_id", "") if model else "",
    }


def verify_code(phone: str, code: str) -> bool:
    """Ask Aliyun to validate a previously sent code for the given phone number."""
    try:
        from alibabacloud_dypnsapi20170525 import models as dypnsapi_models

        response = _client().check_sms_verify_code(
            dypnsapi_models.CheckSmsVerifyCodeRequest(
                phone_number=phone,
                verify_code=code,
                scheme_name=ALIYUN_SMS_VERIFY_SCHEME_NAME or None,
            )
        )
    except SmsVerificationProviderError:
        raise
    except Exception as exc:
        if getattr(exc, "code", "") == "isv.ValidateFail":
            return False
        raise SmsVerificationProviderError(_provider_error_message(exc, "短信核验失败")) from exc

    body = response.body
    if body and body.code == "isv.ValidateFail":
        return False
    if not body or body.code != "OK" or not body.success:
        message = getattr(body, "message", "") if body else ""
        code = getattr(body, "code", "") if body else ""
        detail = f"：{code}{('，' + message) if message else ''}" if code else ""
        raise SmsVerificationProviderError(f"短信核验失败{detail}")
    return bool(body.model and getattr(body.model, "verify_result", "") == "PASS")
