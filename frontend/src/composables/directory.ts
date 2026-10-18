import { api } from './api'
import type { Directory } from '../types/platform'

export type DirectoryCollection = 'teams' | 'profiles' | 'participants' | 'participant_sync' | 'chats' | 'companies'
export type DirectoryMetadata = 'crm_catalog' | 'crm_connection' | 'autonomous_enabled'

export interface DirectoryRequest {
  collections: DirectoryCollection[]
  metadata?: DirectoryMetadata[]
  searches?: Partial<Record<DirectoryCollection, string>>
  includeProjects?: boolean
  projectSearch?: string
  projectPage?: number
  allProjectPages?: boolean
}

type DirectoryPage = Partial<Directory>
type CursorState = Partial<Record<DirectoryCollection, string | null>>

function emptyDirectory(): Directory {
  return { teams: [], profiles: [], projects: [], participants: [], participant_sync: [], chats: [], companies: [], crm_catalog: [] }
}

function nextCursor(page: DirectoryPage, collection: DirectoryCollection): string | null {
  switch (collection) {
    case 'teams': return page.teams_next_cursor ?? null
    case 'profiles': return page.profiles_next_cursor ?? null
    case 'participants': return page.participants_next_cursor ?? null
    case 'participant_sync': return page.participant_sync_next_cursor ?? null
    case 'chats': return page.chats_next_cursor ?? null
    case 'companies': return page.companies_next_cursor ?? null
  }
}

function appendCollection(target: Directory, page: DirectoryPage, collection: DirectoryCollection) {
  switch (collection) {
    case 'teams': target.teams.push(...(page.teams ?? [])); break
    case 'profiles': target.profiles.push(...(page.profiles ?? [])); break
    case 'participants': target.participants?.push(...(page.participants ?? [])); break
    case 'participant_sync': target.participant_sync?.push(...(page.participant_sync ?? [])); break
    case 'chats': target.chats?.push(...(page.chats ?? [])); break
    case 'companies': target.companies?.push(...(page.companies ?? [])); break
  }
}

function pageUrl(collections: DirectoryCollection[], request: DirectoryRequest, cursors: CursorState, projectPage?: number, includeMetadata = true, includeProjectData = true) {
  const params = new URLSearchParams()
  const includes: string[] = [...collections]
  if (includeMetadata) includes.push(...(request.metadata ?? []))
  if (request.includeProjects && includeProjectData) includes.push('projects')
  params.set('include', includes.join(','))
  for (const collection of collections) {
    params.set(`${collection}_limit`, '100')
    const cursor = cursors[collection]
    if (cursor) params.set(`${collection}_cursor`, cursor)
    const search = request.searches?.[collection]?.trim()
    if (search) params.set(`${collection}_search`, search)
  }
  if (request.includeProjects && includeProjectData) {
    params.set('project_page', String(projectPage ?? 1))
    if (request.projectSearch?.trim()) params.set('project_search', request.projectSearch.trim())
  }
  return `/directory/?${params.toString()}`
}

/** Fetches complete, signed-cursor collections and the legacy 100-row project pages. */
export async function loadDirectory(request: DirectoryRequest): Promise<Directory> {
  const collections = [...new Set(request.collections)]
  const first = await api<DirectoryPage>(pageUrl(collections, request, {}, request.projectPage ?? 1))
  const result = Object.assign(emptyDirectory(), first)
  const cursors: CursorState = {}
  for (const collection of collections) cursors[collection] = nextCursor(first, collection)

  while (collections.some(collection => cursors[collection])) {
    const active = collections.filter(collection => cursors[collection])
    const page = await api<DirectoryPage>(pageUrl(active, request, cursors, undefined, false, false))
    for (const collection of active) {
      appendCollection(result, page, collection)
      const next = nextCursor(page, collection)
      if (next && next === cursors[collection]) throw new Error('Справочник не продвинул курсор страницы')
      cursors[collection] = next
    }
  }

  if (request.includeProjects && request.allProjectPages) {
    let projectPage = first.projects_next_page ?? null
    const visitedPages = new Set<number>()
    while (projectPage !== null) {
      if (visitedPages.has(projectPage)) throw new Error('Справочник не продвинул страницу проектов')
      visitedPages.add(projectPage)
      const page = await api<DirectoryPage>(pageUrl([], request, {}, projectPage, false, true))
      result.projects.push(...(page.projects ?? []))
      projectPage = page.projects_next_page ?? null
    }
  }

  for (const collection of collections) {
    switch (collection) {
      case 'participants': result.participants_next_cursor = null; break
      case 'participant_sync': result.participant_sync_next_cursor = null; break
      case 'chats': result.chats_next_cursor = null; break
      case 'profiles': result.profiles_next_cursor = null; break
      case 'teams': result.teams_next_cursor = null; break
      case 'companies': result.companies_next_cursor = null; break
    }
  }
  if (request.includeProjects && request.allProjectPages) result.projects_next_page = null
  return result
}
