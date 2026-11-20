/**
 * Thin wrapper over the local HTTP API.
 *
 * Every call goes to 127.0.0.1. The one job this module has beyond `fetch` is
 * turning FastAPI's error shapes into a single Error with a message that can
 * be shown to an operator verbatim -- the backend already writes its errors in
 * plain language, so they should not be swallowed and replaced with
 * "Request failed".
 */

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { cache: 'no-store', ...options });
  } catch (cause) {
    throw new ApiError(
      'Lost contact with the local server. Is it still running in your terminal?',
      0,
    );
  }

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === 'string') {
        message = payload.detail;
      } else if (Array.isArray(payload.detail) && payload.detail.length) {
        // Pydantic validation errors arrive as a list of field problems.
        message = payload.detail.map((d) => d.msg).join('; ');
      }
    } catch {
      /* response had no JSON body; the status line is all we have */
    }
    throw new ApiError(message, response.status);
  }

  if (response.status === 204) return null;
  return response.json();
}

const asJson = (body) => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

export const api = {
  config: () => request('/api/config'),

  getProject: () => request('/api/project'),

  createProject: (name = 'Untitled board') =>
    request('/api/project', asJson({ name })),

  patchProject: (patch) =>
    request('/api/project', { ...asJson(patch), method: 'PATCH' }),

  uploadReference(file) {
    const form = new FormData();
    form.append('file', file);
    return request('/api/project/reference', { method: 'POST', body: form });
  },

  saveParts: (parts) =>
    request('/api/project/parts', { ...asJson({ parts }), method: 'PUT' }),

  suggest: () => request('/api/project/suggest', { method: 'POST' }),

  namingStatus: () => request('/api/naming/status'),

  suggestName: (partId) =>
    request('/api/project/suggest-name', asJson({ part_id: partId })),

  inspect(files, sensitivity) {
    const form = new FormData();
    for (const file of files) form.append('files', file);
    const query = sensitivity ? `?sensitivity=${encodeURIComponent(sensitivity)}` : '';
    return request(`/api/inspect${query}`, { method: 'POST', body: form });
  },

  listRuns: () => request('/api/runs'),

  getRun: (runId) => request(`/api/runs/${encodeURIComponent(runId)}`),

  clearRuns: () => request('/api/runs', { method: 'DELETE' }),

  exportProject: () => request('/api/project/export'),

  importProject(file) {
    const form = new FormData();
    form.append('file', file);
    return request('/api/project/import', { method: 'POST', body: form });
  },
};
