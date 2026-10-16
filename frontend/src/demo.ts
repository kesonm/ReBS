export interface DemoStatus {
  environment: 'demo'; readOnly: boolean; spotTrading: boolean
  credentialsConfigured: boolean; accessConfigured: boolean; refreshSeconds: number
}
export type AccountRow = Record<string,string>
export interface DemoOverview {
  environment: 'demo'; readOnly: boolean; fetchedAt: number; accountTime: string
  totalEquity: string | null; availableEquity: string | null
  balances: AccountRow[]; positions: AccountRow[] | null; pending: AccountRow[] | null
  history: AccountRow[] | null; fills: AccountRow[] | null
  warnings: { section: string; message: string }[]
}
export interface DemoResult {
  environment: 'demo'; outcome: 'accepted'|'confirmed'|'rejected'|'unknown'
  instId: string; clOrdId?: string; ordId?: string; state?: string; message: string
}
export interface DemoOrder {
  instId: string; side: 'buy'|'sell'; ordType: 'limit'|'market'
  sz: string; px?: string; clOrdId: string
}
export class AccountError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}
export const getDemoStatus = async (signal?: AbortSignal): Promise<DemoStatus> => {
  const response = await fetch('/api/okx/demo/status',{signal,cache:'no-store'})
  if (!response.ok) throw new AccountError('无法读取模拟盘连接状态。',response.status)
  return response.json()
}
export async function demoRequest<T>(token: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api/okx/demo/${path}`,{
    method:body ? 'POST':'GET',cache:'no-store',credentials:'omit',signal,
    headers:{Authorization:`Bearer ${token}`,...(body ? {'Content-Type':'application/json'}:{})},
    ...(body ? {body:JSON.stringify(body)}:{}),
  })
  if (!response.ok) {
    let message=`模拟盘请求失败（${response.status}）`
    try {
      const result=await response.json()
      if (typeof result.detail === 'string') message=result.detail
      else if (response.status===422) message='委托参数格式不正确，请检查价格和数量。'
    } catch { /* Keep proxy errors readable. */ }
    throw new AccountError(message,response.status)
  }
  return response.json()
}
