export interface FinancePayment {id:number; project_id:number; amount:string; currency:string; payment_date:string}
export interface FinanceTarget {id:number; team_id:number; profile_id:number; month:string; amount:string; currency:string; version:number}
export interface FinanceSchedule {id:number; project_id:number; amount:string; currency:string; due_date:string}
export interface FinanceData {
  receivables:{buckets:Record<string,string>;overdue:string;currency:string;unknown_schedule_projects:number}
  payments:FinancePayment[]; targets:FinanceTarget[]; schedules:FinanceSchedule[]
  allocations:{id:number;financial_record_id:number;schedule_item_id:number;amount:string}[]
}
export interface AttachmentItem {id:number;project_id:number;content_type:string;state:string;published_to_client:boolean;created_at:string}
