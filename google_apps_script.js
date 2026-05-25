const SECRET_TOKEN = 'REPLACE_WITH_YOUR_SECRET_TOKEN';

function doPost(e) {
  try {
    const payload = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    const token = String(payload.token || '');
    const subject = String(payload.subject || '').trim();
    const htmlBody = String(payload.htmlBody || '').trim();
    const recipients = Array.isArray(payload.recipients)
      ? payload.recipients.map(String).map(s => s.trim()).filter(Boolean)
      : String(payload.recipients || '')
          .split(',')
          .map(s => s.trim())
          .filter(Boolean);

    if (token !== SECRET_TOKEN) {
      return jsonResponse({ ok: false, error: 'Invalid token' });
    }
    if (!subject) {
      return jsonResponse({ ok: false, error: 'Missing subject' });
    }
    if (!htmlBody) {
      return jsonResponse({ ok: false, error: 'Missing htmlBody' });
    }
    if (!recipients.length) {
      return jsonResponse({ ok: false, error: 'No recipients supplied' });
    }

    GmailApp.sendEmail(recipients.join(','), subject, stripHtml_(htmlBody), {
      htmlBody: htmlBody,
      name: 'Morning Intelligence Tracker',
    });

    return jsonResponse({ ok: true, sent: recipients });
  } catch (err) {
    return jsonResponse({ ok: false, error: String(err) });
  }
}

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function stripHtml_(html) {
  return String(html || '')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}
