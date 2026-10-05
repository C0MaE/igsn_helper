from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY, TA_RIGHT


PDF_OUTPUT_PATH = Path("./pdf")
PDF_OUTPUT_PATH.mkdir(exist_ok=True)


def generate_igsn_pdf(igsn_data, filename=None, generated_by_ai=True):
    """Generate a comprehensive PDF from IGSN JSON data."""

    if not filename:
        title = ""
        if igsn_data.get('titles'):
            title = igsn_data['titles'][0].get('title', '')
            title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
            title = title[:40]
        filename = f"igsn_{title}.pdf" if title else "igsn_document.pdf"

    output_file = PDF_OUTPUT_PATH / filename

    doc = SimpleDocTemplate(
        str(output_file),
        pagesize=A4,
        rightMargin=0.5*inch,
        leftMargin=0.5*inch,
        topMargin=0.5*inch,
        bottomMargin=0.5*inch
    )

    elements = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=16,
        textColor=colors.HexColor('#1f4788'),
        spaceAfter=6,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold'
    )

    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        fontSize=11,
        textColor=colors.HexColor('#1f4788'),
        spaceAfter=6,
        spaceBefore=8,
        fontName='Helvetica-Bold'
    )

    subheading_style = ParagraphStyle(
        'CustomSubHeading',
        parent=styles['Heading3'],
        fontSize=10,
        textColor=colors.HexColor('#333333'),
        spaceAfter=4,
        spaceBefore=4,
        fontName='Helvetica-Bold'
    )

    normal_style = ParagraphStyle(
        'CustomNormal',
        parent=styles['Normal'],
        fontSize=9,
        alignment=TA_JUSTIFY,
        spaceAfter=4
    )

    small_style = ParagraphStyle(
        'CustomSmall',
        parent=styles['Normal'],
        fontSize=8,
        alignment=TA_LEFT,
        spaceAfter=2
    )

    if igsn_data.get('titles'):
        title_text = igsn_data['titles'][0].get('title', 'Untitled')
        elements.append(Paragraph(title_text, title_style))
        elements.append(Spacer(1, 0.1*inch))

        alt_titles = [t for t in igsn_data.get('titles', []) if t.get('titleType') == 'AlternativeTitle']
        if alt_titles:
            for alt_title in alt_titles:
                elements.append(Paragraph(f"<i>Alternative: {alt_title.get('title')}</i>", small_style))

        elements.append(Spacer(1, 0.15*inch))

    metadata_list = []
    if igsn_data.get('doi'):
        metadata_list.append(["DOI:", igsn_data['doi']])
    if igsn_data.get('url'):
        metadata_list.append(["URL:", igsn_data['url']])
    if igsn_data.get('publicationYear'):
        metadata_list.append(["Publication Year:", igsn_data['publicationYear']])

    if metadata_list:
        meta_table = Table(metadata_list, colWidths=[1.5*inch, 4.5*inch])
        meta_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#e8f0f8')),
            ('WORDWRAP', (1, 0), (1, -1), True),
        ]))
        elements.append(meta_table)
        elements.append(Spacer(1, 0.15*inch))

    if igsn_data.get('creators'):
        elements.append(Paragraph("Authors/Creators", heading_style))
        for idx, creator in enumerate(igsn_data['creators'], 1):
            name = creator.get('name', 'N/A')
            given_name = creator.get('givenName', '')
            family_name = creator.get('familyName', '')

            affiliation_text = ""
            if creator.get('affiliation'):
                affiliations = [a.get('name', '') for a in creator['affiliation']]
                affiliation_text = "; ".join(affiliations)

            orcid_text = ""
            if creator.get('nameIdentifiers'):
                for id_obj in creator['nameIdentifiers']:
                    if id_obj.get('nameIdentifierScheme') == 'ORCID':
                        orcid_id = id_obj.get('nameIdentifier', '').replace('https://orcid.org/', '')
                        orcid_text = f"ORCID: {orcid_id}"

            author_info = f"<b>{idx}. {name}</b>"
            if given_name or family_name:
                author_info += f" ({given_name} {family_name})"
            elements.append(Paragraph(author_info, small_style))

            if affiliation_text:
                elements.append(Paragraph(f"<i>Affiliation: {affiliation_text}</i>", small_style))
            if orcid_text:
                elements.append(Paragraph(orcid_text, small_style))

            elements.append(Spacer(1, 0.05*inch))

        elements.append(Spacer(1, 0.1*inch))

    if igsn_data.get('contributors'):
        contributors = [c for c in igsn_data['contributors'] if c]
        if contributors:
            elements.append(Paragraph("Contributors", heading_style))
            for contributor in contributors:
                if contributor.get('name'):
                    contrib_type = contributor.get('contributorType', '')
                    contrib_text = f"<b>{contributor.get('name')}</b>"
                    if contrib_type:
                        contrib_text += f" ({contrib_type})"
                    elements.append(Paragraph(contrib_text, small_style))

                    if contributor.get('affiliation'):
                        affiliations = [a.get('name', '') for a in contributor['affiliation']]
                        aff_text = "; ".join(affiliations)
                        elements.append(Paragraph(f"<i>Affiliation: {aff_text}</i>", small_style))

                    elements.append(Spacer(1, 0.04*inch))

            elements.append(Spacer(1, 0.1*inch))

    if igsn_data.get('descriptions'):
        descriptions = [d for d in igsn_data['descriptions'] if d.get('description')]
        if descriptions:
            elements.append(Paragraph("Description", heading_style))
            for desc in descriptions:
                desc_text = desc.get('description', '')
                if desc_text:
                    desc_type = desc.get('descriptionType', '')
                    if desc_type:
                        elements.append(Paragraph(f"<b>{desc_type}:</b> {desc_text}", normal_style))
                    else:
                        elements.append(Paragraph(desc_text, normal_style))
            elements.append(Spacer(1, 0.1*inch))

    if igsn_data.get('subjects'):
        keywords = [subj.get('subject', '') for subj in igsn_data['subjects'] if subj.get('subject')]
        if keywords:
            elements.append(Paragraph("Keywords", heading_style))
            keywords_text = ", ".join(keywords)
            elements.append(Paragraph(keywords_text, normal_style))
            elements.append(Spacer(1, 0.1*inch))

    elements.append(Paragraph("Resource Information", heading_style))
    resource_info = []
    if igsn_data.get('types'):
        resource_info.append(["Resource Type:", igsn_data['types'].get('resourceType', 'N/A')])
        resource_info.append(["Resource General:", igsn_data['types'].get('resourceTypeGeneral', 'N/A')])
    if igsn_data.get('schemaVersion'):
        resource_info.append(["Schema Version:", igsn_data['schemaVersion']])

    if resource_info:
        res_table = Table(resource_info, colWidths=[1.2*inch, 4.8*inch])
        res_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#e8f0f8')),
        ]))
        elements.append(res_table)
        elements.append(Spacer(1, 0.1*inch))

    if igsn_data.get('publisher'):
        pub = igsn_data['publisher']
        if pub.get('name'):
            elements.append(Paragraph("Publisher", heading_style))
            pub_info = [["Name:", pub['name']]]
            if pub.get('publisherIdentifier'):
                pub_info.append(["Identifier:", pub['publisherIdentifier']])
            if pub.get('publisherIdentifierScheme'):
                pub_info.append(["Identifier Scheme:", pub['publisherIdentifierScheme']])

            pub_table = Table(pub_info, colWidths=[1.2*inch, 4.8*inch])
            pub_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#e8f0f8')),
            ]))
            elements.append(pub_table)
            elements.append(Spacer(1, 0.1*inch))

    if igsn_data.get('dates'):
        dates = [d for d in igsn_data['dates'] if d.get('date')]
        if dates:
            elements.append(Paragraph("Dates", heading_style))
            for date_entry in dates:
                date_text = date_entry.get('date', '')
                date_type = date_entry.get('dateType', '')
                date_info = date_entry.get('dateInformation', '')

                date_str = f"<b>{date_type}:</b> {date_text}"
                if date_info:
                    date_str += f" ({date_info})"
                elements.append(Paragraph(date_str, small_style))
            elements.append(Spacer(1, 0.1*inch))

    if igsn_data.get('relatedIdentifiers'):
        related = [r for r in igsn_data['relatedIdentifiers'] if r.get('relatedIdentifier')]
        if related:
            elements.append(Paragraph("Related Identifiers", heading_style))
            for rel_id in related:
                rel_text = f"<b>{rel_id.get('relationType', '')}:</b> {rel_id.get('relatedIdentifier', '')}"
                rel_type = rel_id.get('relatedIdentifierType', '')
                if rel_type:
                    rel_text += f" ({rel_type})"
                elements.append(Paragraph(rel_text, small_style))
            elements.append(Spacer(1, 0.1*inch))

    if generated_by_ai:
        footer_style = ParagraphStyle(
            'Footer',
            parent=styles['Normal'],
            fontSize=7,
            textColor=colors.HexColor('#999999'),
            alignment=TA_RIGHT,
            spaceAfter=0
        )
        elements.append(Spacer(1, 1*inch))
        elements.append(Paragraph("<i>Generated by AI</i>", footer_style))

    doc.build(elements)
    return output_file
