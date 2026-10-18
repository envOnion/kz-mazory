/** Parse the integer ID contract at the URL boundary, rejecting lossy values. */
export function routeId(value: unknown): number | null {
  if (typeof value !== 'string' || !/^[1-9][0-9]*$/.test(value)) return null
  const parsed: unknown = JSON.parse(value)
  return typeof parsed === 'number' && Number.isSafeInteger(parsed) ? parsed : null
}
