import { reactive } from 'vue'
export const session = reactive({token:'', org:'', user:null as any, memberships:[] as any[]})
let refreshing: Promise<any> | null = null
const csrf = () => decodeURIComponent(document.cookie.split('; ').find(x=>x.startsWith('csrf_token='))?.slice(11) || '')
export class ApiError extends Error { constructor(public code:string, message:string, public details:any){super(message)} }
export async function api(path:string, options:RequestInit={}, retry=true):Promise<any> {
  const headers:Record<string,string> = {'X-Organization-ID':session.org, 'X-CSRF-Token':csrf(), ...(options.headers as Record<string,string> || {})}
  if(session.token) headers.Authorization = `Bearer ${session.token}`
  if(options.body && !(options.body instanceof FormData)) headers['Content-Type']='application/json'
  const response = await fetch('/api/v1'+path, {...options,headers,credentials:'same-origin'})
  if(response.status===401 && retry && !path.startsWith('/auth/')) {
    try { await restore(); return api(path, options, false) } catch { session.token='';session.user=null }
  }
  if(!response.ok) {const e = await response.json();throw new ApiError(e.code,e.message,e.details)}
  if(path.endsWith('/download')) return response.blob()
  return response.json()
}
export const post = (path:string,body:any={},key?:string)=>api(path,{method:'POST',body:JSON.stringify(body),headers:key?{'Idempotency-Key':key}:{}})
export const put = (path:string,body:any)=>api(path,{method:'PUT',body:JSON.stringify(body)})
export const key = ()=>crypto.randomUUID()
export async function restore(){
  if(!refreshing) refreshing=post('/auth/refresh').then(r=>{session.token=r.access_token;session.user=r.user}).finally(()=>{refreshing=null})
  return refreshing
}
export async function loadMemberships(){session.memberships=(await api('/memberships')).items;session.org=session.memberships[0]?.org_id||''}
export const hasRole = (...roles:string[])=>session.memberships.find(m=>m.org_id===session.org)?.roles.some((r:string)=>roles.includes(r))||false
export const date = (s:string)=>s?new Date(s).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—'
export const labels:Record<string,string>={SUCCEEDED:'计算完成',RUNNING:'处理中',QUEUED:'排队中',FAILED:'执行失败',CANCEL_REQUESTED:'取消中',CANCELLED:'已取消',RECOMMENDED:'推荐匹配',REVIEW:'需复核',CONFLICT:'规格冲突',NO_CANDIDATE:'暂无候选',PENDING:'未处理',CONFIRMED:'已确认',UNMATCHED:'当前未匹配',NEEDS_INFO:'待补充',REVOKED:'已撤销',PUBLISHED:'已发布',DRAFT:'待发布',INVALID:'校验失败',operator:'数据专员',reviewer:'审核员',admin:'管理员',viewer:'只读成员'}
export const fields:Record<string,string>={sku:'商品编号',name:'商品名称',brand:'品牌',model:'型号',specs:'规格描述',ram:'运行内存',storage:'存储容量',color:'颜色',region:'销售版本',pack_count:'包装数量',price:'报价',currency:'币种'}
