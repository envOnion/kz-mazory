export interface McpConnection {
  id: number
  token: string
  created_at: string
  last_used_at: string | null
  expires_at: string | null
}
export interface McpConnectionResponse { connection: McpConnection | null }
export type McpConnectionAction = 'create' | 'rotate' | 'revoke'
