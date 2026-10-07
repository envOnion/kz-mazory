"""Local, bounded parsing of WhatsApp TXT exports. Message text is never logged."""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_BYTES = 20 * 1024 * 1024
MAX_MESSAGES = 100000
HEADER = re.compile(r'^(?:\[)?(?P<date>\d{1,2}[./]\d{1,2}[./]\d{2,4}),?\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)(?:\]\s*|\s+[-–]\s+)(?P<body>.*)$')
DATE_START = re.compile(r'^\[?\d{1,2}[./]\d{1,2}[./]\d{2,4}[,\s]')
MEDIA = re.compile(r'^(?:<media omitted>|<без медиафайлов>|<медиафайлы не включены>|изображение отсутствует|видео отсутствует|аудио отсутствует|стикер отсутствует|документ отсутствует|[^\n]*\(файл добавлен\))$', re.I)


class ExportError(ValueError):
    def __init__(self, code, description, line=0):
        self.code, self.description, self.line = code, description, line
        super().__init__(code)


def export_zone(name):
    match = re.fullmatch(r'UTC([+-])(\d{2}):(\d{2})', name)
    if match:
        hours, minutes = int(match[2]), int(match[3])
        if hours <= 14 and minutes < 60 and (hours < 14 or minutes == 0):
            return timezone((1 if match[1] == '+' else -1)*timedelta(hours=hours, minutes=minutes))
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        raise ExportError('export_timezone_invalid', 'Укажите часовой пояс IANA или UTC±HH:MM.') from None


@dataclass(frozen=True)
class ExportRecord:
    ordinal: int
    line_start: int
    line_end: int
    sent_at: datetime
    precision: str
    sender: str
    phone: str
    content: str
    kind: str

    @property
    def fingerprint(self):
        value=[self.sent_at.isoformat(),self.precision,self.sender,self.content]
        return hashlib.sha256(json.dumps(value,ensure_ascii=False).encode()).hexdigest()


def parse_export(data, zone_name, date_order='DMY'):
    if len(data) > MAX_BYTES:
        raise ExportError('export_too_large', 'Размер TXT не должен превышать 20 МиБ.')
    if date_order not in ('DMY','MDY'):
        raise ExportError('export_date_order_invalid','Выберите порядок даты: день/месяц/год или месяц/день/год.')
    zone=export_zone(zone_name)
    try:
        encoding='utf-16' if data[:2] in (b'\xff\xfe',b'\xfe\xff') else 'utf-8-sig'
        text=data.decode(encoding).replace('\r\n','\n').replace('\r','\n')
    except UnicodeError:
        raise ExportError('export_encoding_invalid','Нужен TXT в UTF-8 или UTF-16 с BOM.') from None
    if '\x00' in text or any(ord(char)<32 and char not in '\n\t' for char in text):
        raise ExportError('export_binary_content','Файл содержит бинарные или недопустимые управляющие символы.')
    records=[]; current=None; body=[]; start=0
    def finish(end):
        if current is None:return
        stamp,precision,sender,phone,initial=current
        content='\n'.join([initial]+body)
        kind='system' if not sender else 'media' if MEDIA.fullmatch(content.strip()) or not content.strip() else 'text'
        records.append(ExportRecord(len(records)+1,start,end,stamp,precision,sender,phone,content,kind))
    lines=text.split('\n')
    if lines and lines[-1]=='':lines.pop()
    for number,line in enumerate(lines,1):
        structural=line.lstrip('\u200e\u200f\ufeff')
        match=HEADER.fullmatch(structural)
        if match:
            finish(number-1)
            if len(records)>=MAX_MESSAGES:raise ExportError('export_too_many_messages','В экспорте больше 100000 сообщений.',number)
            pieces=re.split(r'[./]',match['date']);a,b,year=map(int,pieces)
            day,month=(a,b) if date_order=='DMY' else (b,a)
            if len(pieces[2])==2:year+=2000
            clock=match['time'].upper().strip();meridiem=re.search(r'\s*([AP]M)$',clock)
            fields=list(map(int,re.sub(r'\s*[AP]M$','',clock).split(':')))
            hour,minute=fields[:2];second=fields[2] if len(fields)==3 else 0
            if meridiem:
                if not 1<=hour<=12:raise ExportError('export_date_invalid','Некорректное время сообщения.',number)
                hour=hour%12+(12 if meridiem[1]=='PM' else 0)
            try:
                if not 2000<=year<=2099:raise ValueError()
                stamp=datetime(year,month,day,hour,minute,second,tzinfo=zone)
                if stamp.replace(fold=0).utcoffset()!=stamp.replace(fold=1).utcoffset():raise ValueError()
                if stamp.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)!=stamp.replace(tzinfo=None):raise ValueError()
            except ValueError:
                raise ExportError('export_date_invalid','Некорректная или неоднозначная дата/время сообщения.',number) from None
            remainder=match['body'];author,separator,content=remainder.partition(': ')
            sender=author.strip() if separator else ''
            if separator and (not sender or len(sender)>255):raise ExportError('export_sender_invalid','Некорректный автор сообщения.',number)
            phone=re.sub(r'[ ()-]','',sender) if re.fullmatch(r'\+?\d[\d ()-]{5,30}',sender) else ''
            current=(stamp,'second' if len(fields)==3 else 'minute',sender,phone,content if separator else remainder)
            body=[];start=number
        elif DATE_START.match(structural):
            raise ExportError('export_header_invalid','Строка похожа на заголовок, но имеет неподдерживаемый формат.',number)
        elif current is None:
            if structural.strip():raise ExportError('export_preamble_unrecognized','Текст перед первым заголовком сообщения не распознан.',number)
        else:body.append(line)
    finish(len(lines))
    if not records:raise ExportError('export_empty','В файле не найдены сообщения WhatsApp.')
    return records,encoding
