# Voice & Image Support for OpenClaw Bot

## Overview

Add support for voice messages and images sent via Telegram (and other channels)
to the OpenClaw bot. Use Amazon Bedrock Nova models for multimodal processing —
no separate Transcribe service needed.

## Current State

The router Lambda (`lambda/router/index.py`) only processes text messages.
Non-text content is silently ignored:

```python
text = msg.get("text", "")
if not text:
    return None  # voice, photos, stickers all dropped here
```

---

## Requirements

### Voice Messages
- User sends a voice note to the bot on Telegram
- Bot transcribes and understands the audio
- Bot responds with text (and optionally a voice reply)

### Images
- User sends a photo to the bot
- Bot can see and describe/analyze the image
- Bot responds based on image content + any caption

### Constraints
- Cost-effective: use Bedrock Nova for audio/vision (not separate services)
- No changes to the AgentCore container or openclaw CLI
- Processing happens in the router Lambda before calling AgentCore
- Lambda timeout must accommodate download + Bedrock call (~15-20s)

---

## Architecture

```
User sends voice/image
        │
        ▼
Router Lambda
  1. Detect message type (voice/photo)
  2. Download file from Telegram
  3. Call Bedrock Nova (audio/vision) to convert to text
  4. Pass transcribed/described text to AgentCore as normal
        │
        ▼
AgentCore → openclaw → Claude
```

The key insight: **convert media to text in the router**, then the rest of the
pipeline (AgentCore, openclaw, Claude) works unchanged.

---

## Implementation Plan

### Phase 1 — Stabilization (prerequisite)

Before adding features, ensure the bot is stable:

- [ ] Merge PR #5 on openclaw repo (credential expiry fix in entrypoint.sh)
- [ ] Deploy new Docker Hub image: `just deploy-phase2`
- [ ] Verify bot responds consistently for 24+ hours without manual restarts
- [ ] Add automatic session recovery: if AgentCore returns 500, stop the session
      and retry once automatically

### Phase 2 — Voice Messages

**Files to modify:** `lambda/router/index.py`

**Changes:**

1. **Detect voice messages** in `_extract_message()`:
   ```python
   # Telegram sends voice as msg["voice"] with file_id
   voice = msg.get("voice") or msg.get("audio")
   if voice:
       file_id = voice["file_id"]
       # transcribe and use as text
   ```

2. **Download from Telegram** using Bot API:
   ```
   GET https://api.telegram.org/bot{token}/getFile?file_id={file_id}
   → returns file_path
   GET https://api.telegram.org/file/bot{token}/{file_path}
   → returns audio bytes (OGG/OPUS format)
   ```

3. **Transcribe with Bedrock Nova Lite** (audio input, ~$0.003/min):
   ```python
   bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
   response = bedrock.invoke_model(
       modelId="us.amazon.nova-lite-v1:0",
       body=json.dumps({
           "messages": [{
               "role": "user",
               "content": [
                   {
                       "audio": {
                           "format": "ogg",
                           "source": {"bytes": base64.b64encode(audio_bytes).decode()}
                       }
                   },
                   {"text": "Transcribe this voice message accurately."}
               ]
           }]
       })
   )
   transcribed_text = parse_nova_response(response)
   ```

4. **Pass to AgentCore** as: `"[Voice message]: {transcribed_text}"`

**Lambda timeout:** Increase from 60s to 90s to accommodate download + Bedrock call.

### Phase 3 — Image Support

**Files to modify:** `lambda/router/index.py`

**Changes:**

1. **Detect photos** in `_extract_message()`:
   ```python
   photo = msg.get("photo")  # array of sizes, use largest
   if photo:
       file_id = photo[-1]["file_id"]  # largest resolution
       caption = msg.get("caption", "")
   ```

2. **Download from Telegram** (same pattern as voice).

3. **Describe with Bedrock Nova Lite** (vision input):
   ```python
   response = bedrock.invoke_model(
       modelId="us.amazon.nova-lite-v1:0",
       body=json.dumps({
           "messages": [{
               "role": "user",
               "content": [
                   {
                       "image": {
                           "format": "jpeg",
                           "source": {"bytes": base64.b64encode(image_bytes).decode()}
                       }
                   },
                   {"text": caption or "Describe this image in detail."}
               ]
           }]
       })
   )
   description = parse_nova_response(response)
   ```

4. **Pass to AgentCore** as: `"[Image]: {description}\n[Caption]: {caption}"`
   Or if caption exists: pass caption + image description together.

### Phase 4 — Voice Replies (optional)

If the user sends a voice message, optionally reply with audio:

1. After getting text response from AgentCore, use Amazon Polly to synthesize speech
2. Send audio file back via Telegram `sendVoice` API
3. Cost: Polly Neural ~$0.016/1k characters (~$0.002 per typical response)

This is optional — text replies to voice messages are perfectly acceptable.

---

## Cost Estimate

| Feature | Per message | 20 msgs/day (10 users) |
|---------|-------------|------------------------|
| Voice transcription (Nova Lite) | ~$0.003 | ~$0.06/day = $1.8/month |
| Image description (Nova Lite) | ~$0.001 | ~$0.02/day = $0.6/month |
| Voice replies (Polly, optional) | ~$0.002 | ~$0.04/day = $1.2/month |
| **Total addition** | | **~$2.4–3.6/month** |

---

## Files to Change

| File | Change |
|------|--------|
| `lambda/router/index.py` | Add voice/image detection, download, Bedrock call |
| `stacks/router_stack.py` | Add Bedrock Nova permissions to Lambda role, increase timeout to 90s |
| `cdk.json` | Update `router_lambda_timeout_seconds` to 90 |

No changes needed to:
- AgentCore container / openclaw image
- Phase 1/2/3 CDK stacks
- DynamoDB schema
- Channel setup scripts

---

## Testing Plan

1. Send a voice message → verify transcription appears in CloudWatch logs
2. Send an image with caption → verify description + caption passed to bot
3. Send an image without caption → verify description only
4. Send a text message → verify existing behavior unchanged
5. Test with Slack (different audio format: mp4) and WhatsApp (ogg)

---

## Open Questions

1. **Voice reply**: Do we want the bot to reply with audio, or text only?
2. **Image size limit**: Telegram max photo size is 10MB. Should we cap at 5MB?
3. **Unsupported types**: Stickers, GIFs, documents — reply with "I can only process text, voice, and images" or silently ignore?
