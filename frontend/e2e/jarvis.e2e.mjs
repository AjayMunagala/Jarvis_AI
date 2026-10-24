import { test as base, expect } from '@playwright/test'

const syntheticToken = 'jarvis-e2e-synthetic-token-never-a-secret-0001'
const conversationOne = '00000000-0000-4000-8000-000000000001'
const conversationTwo = '00000000-0000-4000-8000-000000000002'

const test = base.extend({
  pageSafety: [async ({ page }, use) => {
    const pageErrors = []
    const externalOrigins = []
    page.on('pageerror', (error) => pageErrors.push(error.name))
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
        externalOrigins.push(url.origin)
      }
    })
    await use()
    expect(pageErrors).toEqual([])
    expect(externalOrigins).toEqual([])
  }, { auto: true }],
})

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: /good to have you here, ajay/i })).toBeVisible()
})

async function unlock(page) {
  await page.getByRole('button', { name: /unlock to load saved conversations/i }).click()
  await page.getByLabel('Local access token').fill(syntheticToken)
  await page.getByRole('button', { name: 'Unlock', exact: true }).click()
}

function conversationSummary(id, preview, activity) {
  return {
    conversation_id: id,
    created_at: '2026-01-01T00:00:00Z',
    last_activity_at: activity,
    preview,
  }
}

function historyPage(id, userText, assistantText) {
  return {
    conversation_id: id,
    messages: [
      { id: `${id}-user`, conversation_id: id, sequence_number: 1, role: 'user', content: userText, created_at: '2026-01-01T00:00:00Z' },
      { id: `${id}-assistant`, conversation_id: id, sequence_number: 2, role: 'assistant', content: assistantText, created_at: '2026-01-01T00:01:00Z' },
    ],
    has_more: false,
    next_before_sequence: null,
  }
}

async function fulfillConversationList(route, items, offset, hasMore = false) {
  const authorization = await route.request().headerValue('authorization')
  expect(typeof authorization === 'string' && authorization.startsWith('Bearer ')).toBe(true)
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ items, limit: 20, offset, has_more: hasMore }),
  })
}

test('starts both local services and renders the application without page errors', async ({ page }) => {
  await expect(page.getByLabel('Main navigation')).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Message JARVIS' })).toBeVisible()
})

test('inspects real local repository evidence in mock mode and previews an unapplied patch', async ({ page }) => {
  await page.getByRole('button', { name: 'Coding', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Analyze coding request' })).toBeDisabled()
  await page.getByRole('button', { name: 'Unlock coding workspace' }).click()
  await page.getByLabel('Local access token').fill(syntheticToken)
  await page.getByRole('button', { name: 'Unlock', exact: true }).click()
  await page.getByLabel('Engineering request').fill('Explain the README module overview')
  await page.getByLabel(/Repository-relative paths/).fill('README.md')
  const responsePromise = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/coding/analyze')
  await page.getByRole('button', { name: 'Analyze coding request' }).click()
  const response = await responsePromise
  expect(response.status()).toBe(200)
  const body = await response.json()
  expect(body).toMatchObject({ analysis_origin: 'static', verification_status: 'not_run', repository_modified: false, execution_enabled: false })
  expect(body.evidence[0].path).toBe('README.md')
  await expect(page.getByRole('article', { name: 'Coding report' })).toBeVisible()
  await page.getByLabel('Workflow', { exact: true }).selectOption('patch')
  await page.getByLabel(/Repository-relative paths/).fill('coding-smoke-proposed.py')
  await page.getByText('Optional exact changes for offline patch generation').click()
  await page.getByLabel('Proposed changes JSON').fill(JSON.stringify([{ path: 'coding-smoke-proposed.py', operation: 'create', content: 'example = 1\n', reason: 'Synthetic unapplied proposal' }]))
  await page.getByRole('button', { name: 'Analyze coding request' }).click()
  await expect(page.getByLabel('Proposed diff for coding-smoke-proposed.py')).toContainText('+example = 1')
  await expect(page.getByRole('heading', { name: 'Review only — nothing applied' })).toBeVisible()
  await page.getByRole('button', { name: 'Lock workspace' }).click()
  await expect(page.getByRole('article', { name: 'Coding report' })).toHaveCount(0)
  await expect(page.getByLabel('Local access token')).toHaveValue('')
})

test('supports anonymous deterministic mock chat without presenting it as persisted', async ({ page }) => {
  const chatResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/chat')
  await page.getByRole('textbox', { name: 'Message JARVIS' }).fill('E2E offline check')
  await page.getByRole('textbox', { name: 'Message JARVIS' }).press('Enter')

  const response = await chatResponse
  expect(response.status()).toBe(200)
  const body = await response.json()
  expect(body).toMatchObject({ provider: 'mock', demo: true, conversation_id: null })
  await expect(page.getByText('This is a JARVIS demo response, not an AI-generated answer. I received: E2E offline check')).toBeVisible()
  await expect(page.getByText('DEMO · MOCK').first()).toBeVisible()

  await page.reload()
  await expect(page.getByText('Your conversations will appear here.')).toBeVisible()
  await expect(page.getByText('E2E offline check')).toHaveCount(0)
})

test('loads authenticated conversation pages and history, then sends a follow-up to the backend ID', async ({ page }) => {
  let releaseFirstPage
  let announceFirstPage
  const firstPageGate = new Promise((resolve) => { releaseFirstPage = resolve })
  const firstPageRequested = new Promise((resolve) => { announceFirstPage = resolve })

  await page.route('**/api/conversations?*', async (route) => {
    const url = new URL(route.request().url())
    const offset = Number(url.searchParams.get('offset'))
    if (offset === 0) {
      announceFirstPage()
      await firstPageGate
      await fulfillConversationList(route, [conversationSummary(conversationOne, 'First saved question', '2026-01-02T00:00:00Z')], 0, true)
      return
    }
    await fulfillConversationList(route, [conversationSummary(conversationTwo, 'Second saved question', '2026-01-01T00:00:00Z')], 20)
  })
  await page.route('**/api/conversations/*/messages?*', async (route) => {
    expect(new URL(route.request().url()).pathname).toContain(conversationOne)
    await route.fulfill({ json: historyPage(conversationOne, 'Original saved question', 'Earlier saved answer') })
  })
  await page.route('**/api/chat', async (route) => {
    const body = route.request().postDataJSON()
    expect(body).toMatchObject({ message: 'Continue saved conversation', conversation_id: conversationOne })
    await route.fulfill({ json: { reply: 'Follow-up response', provider: 'mock', demo: true, conversation_id: conversationOne } })
  })

  await unlock(page)
  await firstPageRequested
  await expect(page.getByText('Loading saved conversations…')).toBeVisible()
  releaseFirstPage()
  await expect(page.getByTitle('First saved question')).toBeVisible()

  await page.getByRole('button', { name: 'Load more' }).click()
  await expect(page.getByTitle('Second saved question')).toBeVisible()
  await page.getByTitle('First saved question').click()
  await expect(page.getByText('Original saved question')).toBeVisible()
  await expect(page.getByText('Earlier saved answer')).toBeVisible()

  await page.getByRole('textbox', { name: 'Message JARVIS' }).fill('Continue saved conversation')
  await page.getByRole('button', { name: 'Send message' }).click()
  await expect(page.getByText('Follow-up response')).toBeVisible()
})

test('renders the empty state after an authenticated empty conversation list', async ({ page }) => {
  await page.route('**/api/conversations?*', (route) => fulfillConversationList(route, [], 0))
  await unlock(page)
  await expect(page.getByText('No saved conversations yet.')).toBeVisible()
})

test('locking clears the token, draft, selected conversation, and late history responses', async ({ page }) => {
  let releaseHistory
  let announceHistory
  const historyGate = new Promise((resolve) => { releaseHistory = resolve })
  const historyRequested = new Promise((resolve) => { announceHistory = resolve })

  await page.route('**/api/conversations?*', (route) => fulfillConversationList(
    route, [conversationSummary(conversationOne, 'Sensitive sidebar preview', '2026-01-02T00:00:00Z')], 0,
  ))
  await page.route('**/api/conversations/*/messages?*', async (route) => {
    announceHistory()
    await historyGate
    try {
      await route.fulfill({ json: historyPage(conversationOne, 'Sensitive old message', 'Late response content') })
    } catch {
      // Locking aborts the in-flight browser request; the stale result must not restore UI state.
    }
  })

  await unlock(page)
  await page.getByTitle('Sensitive sidebar preview').click()
  await historyRequested
  await page.getByRole('textbox', { name: 'Message JARVIS' }).fill('Draft to clear')
  await page.getByRole('button', { name: 'Lock workspace' }).click()
  await expect(page.getByRole('heading', { name: 'Unlock local workspace' })).toBeVisible()
  await expect(page.getByLabel('Local access token')).toHaveValue('')
  await expect(page.getByTitle('Sensitive sidebar preview')).toHaveCount(0)
  await expect(page.getByText('Sensitive old message')).toHaveCount(0)
  await expect(page.getByRole('textbox', { name: 'Message JARVIS' })).toHaveValue('')

  releaseHistory()
  await expect(page.getByText('Late response content')).toHaveCount(0)
  await expect(page.getByTitle('Sensitive sidebar preview')).toHaveCount(0)
})

test('an HTTP 401 clears displayed conversation data and requires authentication again', async ({ page }) => {
  await page.route('**/api/conversations?*', (route) => fulfillConversationList(
    route, [conversationSummary(conversationOne, 'Protected preview', '2026-01-02T00:00:00Z')], 0,
  ))
  await page.route('**/api/conversations/*/messages?*', (route) => route.fulfill({
    status: 401,
    contentType: 'application/json',
    headers: { 'WWW-Authenticate': 'Bearer' },
    body: JSON.stringify({ error: 'unauthorized', detail: 'A valid bearer credential is required.' }),
  }))

  await unlock(page)
  await page.getByTitle('Protected preview').click()
  await expect(page.getByRole('heading', { name: 'Unlock local workspace' })).toBeVisible()
  await expect(page.getByLabel('Local access token')).toHaveValue('')
  await expect(page.getByTitle('Protected preview')).toHaveCount(0)
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('a stale response for one conversation cannot replace the newly selected conversation', async ({ page }) => {
  let releaseFirstHistory
  let announceFirstHistory
  const firstHistoryGate = new Promise((resolve) => { releaseFirstHistory = resolve })
  const firstHistoryRequested = new Promise((resolve) => { announceFirstHistory = resolve })

  await page.route('**/api/conversations?*', (route) => fulfillConversationList(route, [
    conversationSummary(conversationOne, 'Conversation A', '2026-01-02T00:00:00Z'),
    conversationSummary(conversationTwo, 'Conversation B', '2026-01-01T00:00:00Z'),
  ], 0))
  await page.route('**/api/conversations/*/messages?*', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.includes(conversationOne)) {
      announceFirstHistory()
      await firstHistoryGate
      await route.fulfill({ json: historyPage(conversationOne, 'Conversation A private body', 'Conversation A response') })
      return
    }
    await route.fulfill({ json: historyPage(conversationTwo, 'Conversation B private body', 'Conversation B response') })
  })

  await unlock(page)
  await page.getByTitle('Conversation A').click()
  await firstHistoryRequested
  await page.getByTitle('Conversation B').click()
  await expect(page.getByText('Conversation B private body')).toBeVisible()
  await expect(page.getByText('Conversation B response')).toBeVisible()
  releaseFirstHistory()
  await expect(page.getByText('Conversation A private body')).toHaveCount(0)
  await expect(page.getByText('Conversation B private body')).toBeVisible()
})

test('shows safe missing-conversation and provider errors, with only explicit manual retry', async ({ page }) => {
  let chatCalls = 0
  await page.route('**/api/conversations?*', (route) => fulfillConversationList(
    route, [conversationSummary(conversationOne, 'Conversation no longer present', '2026-01-02T00:00:00Z')], 0,
  ))
  await page.route('**/api/conversations/*/messages?*', (route) => route.fulfill({
    status: 404,
    contentType: 'application/json',
    body: JSON.stringify({ error: 'conversation_not_found', detail: 'The requested conversation was not found.' }),
  }))
  await page.route('**/api/chat', async (route) => {
    chatCalls += 1
    if (chatCalls === 1) {
      await route.fulfill({
        status: 502,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'provider_error', detail: 'private internal diagnostic' }),
      })
      return
    }
    await route.fulfill({ json: { reply: 'Manual retry succeeded', provider: 'mock', demo: true, conversation_id: null } })
  })

  await unlock(page)
  await page.getByTitle('Conversation no longer present').click()
  await expect(page.getByRole('alert')).toHaveText(/conversation is no longer available/i)
  await expect(page.getByText(/private internal diagnostic|sql|password/i)).toHaveCount(0)

  await page.getByRole('button', { name: 'New conversation' }).click()
  await page.getByRole('textbox', { name: 'Message JARVIS' }).fill('Check manual retry')
  await page.getByRole('button', { name: 'Send message' }).click()
  await expect(page.getByText('JARVIS could not complete that request. Please try again.')).toBeVisible()
  await expect(page.getByText(/manual retry may duplicate this exchange/i)).toBeVisible()
  await expect(page.getByText(/private internal diagnostic/i)).toHaveCount(0)
  expect(chatCalls).toBe(1)

  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByText('Manual retry succeeded')).toBeVisible()
  expect(chatCalls).toBe(2)
})

test('keeps memory management and document-ingestion confirmation accessible', async ({ page }) => {
  await page.getByRole('button', { name: 'Memory' }).click()
  await expect(page.getByRole('heading', { name: 'Memory', exact: true })).toBeVisible()
  const fileInput = page.getByLabel('Import one .txt or .md document (16 KiB maximum)')
  await expect(fileInput).toBeVisible()
  await fileInput.setInputFiles({ name: 'e2e-reference.md', mimeType: 'text/markdown', buffer: Buffer.from('Synthetic untrusted document text.') })

  const confirmation = page.getByRole('alertdialog', { name: 'Import this document?' })
  await expect(confirmation).toBeVisible()
  await expect(confirmation).toContainText('e2e-reference.md')
  await page.getByRole('button', { name: 'Cancel' }).click()
  await expect(confirmation).toHaveCount(0)
})

test('offers opt-in browser voice that fills the composer without sending and reads new replies aloud', async ({ page }) => {
  // A deterministic stand-in for the browser Web Speech API: no microphone and no speech service are used.
  await page.addInitScript(() => {
    window.__voice = { recognitions: [], spoken: [] }
    class FakeRecognition {
      constructor() { window.__voice.recognitions.push(this) }
      start() { this.started = true }
      stop() { this.onend?.() }
      abort() { this.onend?.() }
    }
    Object.defineProperty(window, 'SpeechRecognition', { value: FakeRecognition, configurable: true })
    Object.defineProperty(window, 'SpeechSynthesisUtterance', { value: class { constructor(text) { this.text = text } }, configurable: true })
    Object.defineProperty(window, 'speechSynthesis', {
      value: { speak(utterance) { window.__voice.spoken.push(utterance.text); setTimeout(() => utterance.onend?.(), 0) }, cancel() {}, getVoices() { return [] } },
      configurable: true,
    })
  })
  await page.reload()
  let chatRequests = 0
  page.on('request', (request) => { if (new URL(request.url()).pathname === '/api/chat') chatRequests += 1 })

  await page.getByRole('button', { name: 'Start voice input' }).click()
  const disclosure = page.getByRole('dialog', { name: 'Use voice with JARVIS?' })
  await expect(disclosure).toContainText('JARVIS never receives or stores audio')
  await page.getByRole('button', { name: 'Enable voice' }).click()
  await expect(page.getByRole('button', { name: 'Stop voice input' })).toBeVisible()
  await page.evaluate(() => {
    const recognition = window.__voice.recognitions.at(-1)
    recognition.onresult({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: 'E2E voice check' } }] })
    recognition.onend()
  })
  const composer = page.getByRole('textbox', { name: 'Message JARVIS' })
  await expect(composer).toHaveValue('E2E voice check')
  expect(chatRequests).toBe(0)

  await page.getByRole('button', { name: 'Read replies aloud' }).click()
  const chatResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/chat')
  await composer.press('Enter')
  expect((await chatResponse).status()).toBe(200)
  const reply = 'This is a JARVIS demo response, not an AI-generated answer. I received: E2E voice check'
  await expect(page.getByText(reply)).toBeVisible()
  await expect.poll(() => page.evaluate(() => window.__voice.spoken)).toEqual([reply])
  expect(chatRequests).toBe(1)
})

test('v6.7 operations views: schedule a briefing, enroll the mock lamp and activate it through an approval', async ({ page }) => {
  await unlock(page)

  await page.getByRole('button', { name: 'Scheduler', exact: true }).click()
  await expect(page.getByText('No scheduled jobs yet.')).toBeVisible()
  await page.getByLabel('Name').fill('E2E briefing')
  await page.getByLabel('Schedule type').selectOption('interval')
  await page.getByLabel('Schedule expression').fill('3600')
  await page.getByRole('button', { name: 'Create job' }).click()
  const jobs = page.getByRole('region', { name: 'Scheduled jobs' })
  await expect(jobs.getByText('E2E briefing')).toBeVisible()
  await expect(jobs.getByText('daily_briefing')).toBeVisible()

  await page.getByRole('button', { name: 'Integrations', exact: true }).click()
  await page.getByLabel('Display name').fill('E2E lamp')
  await page.getByRole('button', { name: 'Enroll', exact: true }).click()
  await expect(page.getByText(/"E2E lamp" enrolled as pending/)).toBeVisible()
  await page.getByRole('button', { name: 'Activate E2E lamp' }).click()
  await expect(page.getByText(/Approval requested for activating "E2E lamp"/)).toBeVisible()

  await page.getByRole('button', { name: 'Open approvals' }).click()
  await expect(page.getByRole('heading', { name: 'Activity' })).toBeVisible()
  await page.getByRole('button', { name: 'Approve (allows one run)' }).click()
  await expect(page.getByText(/Nothing has run yet/)).toBeVisible()

  await page.getByRole('button', { name: 'Integrations', exact: true }).click()
  await page.getByRole('button', { name: 'Activate with approval' }).click()
  await expect(page.getByText('"E2E lamp" is active.')).toBeVisible()
  const actions = page.getByRole('form', { name: 'Actions for E2E lamp' })
  await actions.getByRole('button', { name: 'Run action' }).click()
  await expect(page.getByText('{"power":"off","brightness":0,"simulated":true}')).toBeVisible()
})
