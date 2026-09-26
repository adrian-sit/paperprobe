const state = { paperId: null };
const statusLine = document.querySelector('#status');
const fetchButton = document.querySelector('#fetch-button');
const questionButton = document.querySelector('#question-button');

function setStatus(message, isError = false) {
  statusLine.textContent = message;
  statusLine.classList.toggle('error', isError);
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === 'object' ? body.detail.message : body.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return body;
}

function renderExtraction(fields) {
  const grid = document.querySelector('#extraction-grid');
  grid.replaceChildren();
  fields.forEach(({ field_type: name, value }) => {
    const card = document.createElement('article'); card.className = 'field';
    const title = document.createElement('h3'); title.textContent = name.replaceAll('_', ' '); card.append(title);
    const values = value.items || (value.text ? [value.text] : []);
    if (values.length) { const list = document.createElement('ul'); values.forEach(item => { const li = document.createElement('li'); li.textContent = typeof item === 'string' ? item : item.statement; list.append(li); }); card.append(list); }
    else { const empty = document.createElement('p'); empty.textContent = 'Not identified from the abstract.'; card.append(empty); }
    grid.append(card);
  });
}

function renderPaper(paper) {
  state.paperId = paper.id;
  document.querySelector('#paper-view').hidden = false;
  document.querySelector('#paper-title').textContent = paper.title || 'Untitled paper';
  document.querySelector('#source-link').href = paper.source_uri;
  document.querySelector('#abstract').textContent = paper.sections.find(section => section.heading === 'Abstract')?.content || 'No abstract stored.';
  document.querySelector('#questions').replaceChildren();
  renderExtraction(paper.extracted_fields);
}

document.querySelector('#ingest-form').addEventListener('submit', async event => {
  event.preventDefault();
  const forumId = document.querySelector('#forum-id').value.trim();
  fetchButton.disabled = true; setStatus('Fetching the paper and extracting fields with Gemini…');
  try { const paper = await request('/api/v1/papers/openreview', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({forum_id: forumId}) }); renderPaper(paper); setStatus(paper.from_cache ? 'Loaded the stored paper—no external API calls were made.' : 'Paper fetched and saved. You can now generate discussion questions.'); }
  catch (error) { setStatus(error.message, true); }
  finally { fetchButton.disabled = false; }
});

questionButton.addEventListener('click', async () => {
  if (!state.paperId) return;
  questionButton.disabled = true; setStatus('Generating questions with Gemini…');
  try { const result = await request(`/api/v1/papers/${state.paperId}/questions`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({count:5}) }); const list = document.querySelector('#questions'); list.replaceChildren(); result.questions.forEach(question => { const li=document.createElement('li'); li.textContent=question.text; list.append(li); }); setStatus('Questions saved to the paper.'); }
  catch (error) { setStatus(error.message, true); }
  finally { questionButton.disabled = false; }
});
