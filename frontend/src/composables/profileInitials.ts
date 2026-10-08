export function profileInitials(name: string): string {
  return name.trim().split(/\s+/).filter(Boolean).slice(0, 2).map(part => part.charAt(0)).join('').toLocaleUpperCase('ru') || 'Я'
}
