"""Compliance types — data classes."""

class ClientRiskLevel(Enum):
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"
    VERY_AGGRESSIVE = "very_aggressive"
    UNKNOWN = "unknown"



class ProductRiskLevel(Enum):
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    R4 = "R4"
    R5 = "R5"



class ReportType(Enum):
    DAILY_TRADE = "daily_trade"
    POSITION_SNAPSHOT = "position_snapshot"
    FUND_FLOW = "fund_flow"
    LARGE_TRADE = "large_trade"
    ABNORMAL_TRADE = "abnormal_trade"
    CLIENT_INFO = "client_info"
    RISK_INDICATOR = "risk_indicator"
    MONTHLY_SUMMARY = "monthly_summary"



class FundAccountType(Enum):
    MARGIN = "margin"
    CASH = "cash"
    CREDIT = "credit"
    DERIVATIVES = "derivatives"
    THIRD_PARTY = "third_party"


@dataclass

class ClientProfile:
    client_id: str
    name: str
    id_type: str = "ID_CARD"
    id_number: str = ""
    phone: str = ""
    email: str = ""
    risk_level: ClientRiskLevel = ClientRiskLevel.UNKNOWN
    assessment_date: Optional[date] = None
    assessment_version: str = "1.0"
    assessment_score: int = 0
    is_professional: bool = False
    is_qualified_investor: bool = False
    financial_assets: float = 0.0
    investment_experience_years: int = 0
    account_ids: List[str] = field(default_factory=list)
    fund_account_type: FundAccountType = FundAccountType.CASH
    status: str = "active"
    blacklisted: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass

class ProductInfo:
    product_code: str
    name: str
    product_type: str
    risk_level: ProductRiskLevel
    issuer: str = ""
    manager: str = ""
    min_investment: float = 0.0
    suitable_clients: List[ClientRiskLevel] = field(default_factory=list)
    restricted_clients: List[str] = field(default_factory=list)
    status: str = "active"
    launch_date: Optional[date] = None
    expiry_date: Optional[date] = None


@dataclass

class SuitabilityCheckResult:
    client_id: str
    product_code: str
    passed: bool
    reason: str = ""
    risk_match: bool = True
    qualification_match: bool = True
    warnings: List[str] = field(default_factory=list)
    checked_at: datetime = field(default_factory=datetime.utcnow)


@dataclass

class RegulatoryReport:
    report_id: str
    report_type: ReportType
    reporting_date: date
    data: Dict[str, Any]
    file_path: str = ""
    file_hash: str = ""
    status: str = "pending"
    submitted_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None
    error_message: str = ""
    retry_count: int = 0


@dataclass

class FundAccount:
    account_id: str
    client_id: str
    account_type: FundAccountType
    broker_name: str
    total_assets: float = 0.0
    available_cash: float = 0.0
    frozen_cash: float = 0.0
    market_value: float = 0.0
    margin_ratio: float = 0.0
    margin_used: float = 0.0
    margin_available: float = 0.0
    status: str = "active"
    currency: str = "CNY"
    custodian: str = ""
    custody_account: str = ""
    last_sync: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass

class AuditLogEntry:
    log_id: str
    timestamp: datetime
    event_type: str
    operator: str
    operator_type: str
    action: str
    resource_type: str
    resource_id: str
    before: Dict[str, Any] = field(default_factory=dict)
    after: Dict[str, Any] = field(default_factory=dict)
    ip_address: str = ""
    user_agent: str = ""
    session_id: str = ""
    result: str = "success"
    error_message: str = ""
    severity: str = "info"



