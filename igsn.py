from typing import List, Literal, Optional
from pydantic import BaseModel, Field


NameType = Literal["Organizational", "Personal"]

TitleType = Literal["AlternativeTitle", "Subtitle", "TranslatedTitle", "Other"]

DescriptionType = Literal[
    "Abstract", "Methods", "SeriesInformation", "TableOfContents", "TechnicalInfo", "Other"
]

# DataCite kernel 4.4. The publication types (JournalArticle, Text, ...) matter
# for relatedIdentifiers: with constrained decoding the model can only pick
# from this list, and without them it labels cited papers as "Dataset".
ResourceTypeGeneral = Literal[
    "PhysicalObject",
    "Audiovisual", "Book", "BookChapter", "Collection", "ComputationalNotebook",
    "ConferencePaper", "ConferenceProceeding", "DataPaper", "Dataset", "Dissertation",
    "Event", "Image", "InteractiveResource", "Journal", "JournalArticle", "Model",
    "OutputManagementPlan", "PeerReview", "Preprint", "Report", "Service", "Software",
    "Sound", "Standard", "Text", "Workflow", "Other"
]

ContributorType = Literal[
    "ContactPerson", "DataCollector", "DataCurator", "DataManager", "Distributor",
    "Editor", "HostingInstitution", "Producer", "ProjectLeader", "ProjectManager",
    "ProjectMember", "RegistrationAgency", "RegistrationAuthority", "RelatedPerson",
    "Researcher", "ResearchGroup", "RightsHolder", "Sponsor", "Supervisor",
    "WorkPackageLeader", "Other"
]

DateType = Literal[
    "Accepted", "Available", "Copyrighted", "Collected", "Created", "Issued",
    "Submitted", "Updated", "Valid", "Withdrawn", "Other"
]

RelatedIdentifierType = Literal[
    "ARK", "ArXiv", "Bibcode", "DOI", "EAN13", "EISSN", "Handle", "IGSN",
    "ISBN", "ISSN", "ISTC", "LCCN", "PURL", "UPC", "URL", "URN", "w3id"
]

RelationType = Literal[
    "IsCitedBy", "Cites", "IsSupplementTo", "IsSupplementedBy", "IsContinuedBy",
    "Continues", "IsDescribedBy", "Describes", "HasMetadata", "IsMetadataFor",
    "HasVersion", "IsVersionOf", "IsNewVersionOf", "IsPreviousVersionOf",
    "IsPartOf", "HasPart", "IsReferencedBy", "References", "IsDocumentedBy",
    "Documents", "IsCompiledBy", "Compiles", "IsVariantFormOf", "IsOriginalFormOf",
    "IsIdenticalTo", "IsDerivedFrom", "IsSourceOf", "IsObsoletedBy", "Obsoletes"
]


class NameIdentifier(BaseModel):
    nameIdentifier: str
    nameIdentifierScheme: str
    schemeURI: Optional[str] = None


class Affiliation(BaseModel):
    name: str
    affiliationIdentifier: Optional[str] = None
    affiliationIdentifierScheme: Optional[str] = None
    schemeURI: Optional[str] = None


class Creator(BaseModel):
    name: str
    nameType: Optional[NameType] = None
    givenName: Optional[str] = None
    familyName: Optional[str] = None
    nameIdentifiers: List[NameIdentifier] = Field(default_factory=list)
    affiliation: List[Affiliation] = Field(default_factory=list)


class Contributor(BaseModel):
    name: str
    contributorType: ContributorType
    nameType: Optional[NameType] = None
    givenName: Optional[str] = None
    familyName: Optional[str] = None
    nameIdentifiers: List[NameIdentifier] = Field(default_factory=list)
    affiliation: List[Affiliation] = Field(default_factory=list)


class Title(BaseModel):
    title: str
    titleType: Optional[TitleType] = None
    lang: Optional[str] = None


class ResourceType(BaseModel):
    resourceTypeGeneral: ResourceTypeGeneral = "PhysicalObject"
    resourceType: Optional[str] = None  # z.B. "Rock sample", "Borehole core", "Soil"


class Description(BaseModel):
    description: str
    descriptionType: DescriptionType
    lang: Optional[str] = None


class Subject(BaseModel):
    subject: str
    lang: Optional[str] = None
    subjectScheme: Optional[str] = None
    schemeURI: Optional[str] = None
    valueURI: Optional[str] = None


class Publisher(BaseModel):
    name: str
    publisherIdentifier: Optional[str] = None
    publisherIdentifierScheme: Optional[str] = None
    schemeURI: Optional[str] = None


class Date(BaseModel):
    date: str  # YYYY, YYYY-MM-DD oder ISO-8601
    dateType: DateType
    dateInformation: Optional[str] = None


class RelatedIdentifier(BaseModel):
    relatedIdentifier: str
    relatedIdentifierType: RelatedIdentifierType
    relationType: RelationType
    resourceTypeGeneral: Optional[ResourceTypeGeneral] = None


class IGSN(BaseModel):
    doi: str
    url: str
    creators: List[Creator]
    titles: List[Title]
    publisher: Publisher
    publicationYear: str
    types: ResourceType = Field(default_factory=ResourceType)
    schemaVersion: str = "http://datacite.org/schema/kernel-4"
    contributors: List[Contributor] = Field(default_factory=list)
    dates: List[Date] = Field(default_factory=list)
    language: Optional[str] = None
    descriptions: List[Description] = Field(default_factory=list)
    subjects: List[Subject] = Field(default_factory=list)
    relatedIdentifiers: List[RelatedIdentifier] = Field(default_factory=list)

# --- What the model is asked to fill ------------------------------------------
#
# The extraction schema deliberately leaves out everything that is policy
# rather than content: publisher, schemaVersion, publicationYear,
# resourceTypeGeneral, doi/url and contributors are set in postprocess.py.
# Affiliations carry no identifier fields, because RORs rarely appear in
# submissions and a small model fills them by copying example IDs.
# Fields without defaults are required, so the model cannot skip them.
#
# One document often describes several samples (a table of 17 pellets, six
# powder mixtures), and each sample gets its own IGSN. What all samples share
# (people, common keywords, dates, references) is extracted once, so the
# output stays short even for many samples; postprocess.py merges it into
# one record per sample.


class ExtractedAffiliation(BaseModel):
    name: str


class ExtractedCreator(BaseModel):
    name: str
    givenName: str
    familyName: str
    role: str  # as written in the document; mapped to contributorType in code
    nameIdentifiers: List[NameIdentifier]
    affiliation: List[ExtractedAffiliation]


class SharedMetadata(BaseModel):
    creators: List[ExtractedCreator]
    preparation: str
    subjects: List[Subject]
    dates: List[Date]
    relatedIdentifiers: List[RelatedIdentifier]


class ExtractedSample(BaseModel):
    # Pass 1 only copies. Writing a title or abstract where the document has
    # none made the model loop (it repeated the document's first line), so
    # prose is written per sample in pass 2 (SampleText) instead.
    sampleCode: str
    givenTitle: str
    givenDescription: str
    facts: List[str]
    subjects: List[Subject]
    dates: List[Date]
    relatedIdentifiers: List[RelatedIdentifier]


class IGSNExtraction(BaseModel):
    shared: SharedMetadata
    samples: List[ExtractedSample]


# --- What the model writes in pass 2, one call per sample ----------------------


class SampleText(BaseModel):
    title: str
    abstract: str
    keywords: List[str]
    issues: List[str]
