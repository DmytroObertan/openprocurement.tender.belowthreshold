# -*- coding: utf-8 -*-
from barbecue import chef
from base64 import b64decode
from datetime import datetime, time, timedelta
from logging import getLogger
from openprocurement.api.constants import TZ, WORKING_DAYS
from pkg_resources import get_distribution
from urllib import unquote
from urlparse import urlparse, parse_qsl
from openprocurement.api.utils import get_now, context_unpack
from openprocurement.tender.core.utils import (
    ACCELERATOR_RE, error_handler, calculate_business_date
)
from openprocurement.tender.core.constants import COMPLAINT_STAND_STILL_TIME

PKG = get_distribution(__package__)
LOGGER = getLogger(PKG.project_name)


def cleanup_bids_for_cancelled_lots(tender):
    cancelled_lots = [i.id for i in tender.lots if i.status == 'cancelled']
    if cancelled_lots:
        return
    cancelled_items = [i.id for i in tender.items if i.relatedLot in cancelled_lots]
    cancelled_features = [
        i.code
        for i in (tender.features or [])
        if i.featureOf == 'lot' and i.relatedItem in cancelled_lots or i.featureOf == 'item' and i.relatedItem in cancelled_items
    ]
    for bid in tender.bids:
        bid.documents = [i for i in bid.documents if i.documentOf != 'lot' or i.relatedItem not in cancelled_lots]
        bid.parameters = [i for i in bid.parameters if i.code not in cancelled_features]
        bid.lotValues = [i for i in bid.lotValues if i.relatedLot not in cancelled_lots]
        if not bid.lotValues:
            tender.bids.remove(bid)


def remove_draft_bids(request):
    tender = request.validated['tender']
    if [bid for bid in tender.bids if getattr(bid, "status", "active") == "draft"]:
        LOGGER.info('Remove draft bids',
                    extra=context_unpack(request, {'MESSAGE_ID': 'remove_draft_bids'}))
        tender.bids = [bid for bid in tender.bids if getattr(bid, "status", "active") != "draft"]


def check_bids(request):
    tender = request.validated['tender']
    if tender.lots:
        [setattr(i.auctionPeriod, 'startDate', None) for i in tender.lots if i.numberOfBids < 2 and i.auctionPeriod and i.auctionPeriod.startDate]
        [setattr(i, 'status', 'unsuccessful') for i in tender.lots if i.numberOfBids == 0 and i.status == 'active']
        cleanup_bids_for_cancelled_lots(tender)
        if not set([i.status for i in tender.lots]).difference(set(['unsuccessful', 'cancelled'])):
            tender.status = 'unsuccessful'
        elif max([i.numberOfBids for i in tender.lots if i.status == 'active']) < 2:
            add_next_award(request)
    else:
        if tender.numberOfBids < 2 and tender.auctionPeriod and tender.auctionPeriod.startDate:
            tender.auctionPeriod.startDate = None
        if tender.numberOfBids == 0:
            tender.status = 'unsuccessful'
        if tender.numberOfBids == 1:
            #tender.status = 'active.qualification'
            add_next_award(request)


def check_document(request, document, document_container, route_kwargs):
    url = document.url
    parsed_url = urlparse(url)
    parsed_query = dict(parse_qsl(parsed_url.query))
    if not url.startswith(request.registry.docservice_url) or \
            len(parsed_url.path.split('/')) != 3 or \
            set(['Signature', 'KeyID']) != set(parsed_query):
        request.errors.add(document_container, 'url', "Can add document only from document service.")
        request.errors.status = 403
        raise error_handler(request.errors)
    if not document.hash:
        request.errors.add(document_container, 'hash', "This field is required.")
        request.errors.status = 422
        raise error_handler(request.errors)
    keyid = parsed_query['KeyID']
    if keyid not in request.registry.keyring:
        request.errors.add(document_container, 'url', "Document url expired.")
        request.errors.status = 422
        raise error_handler(request.errors)
    dockey = request.registry.keyring[keyid]
    signature = parsed_query['Signature']
    key = urlparse(url).path.split('/')[-1]
    try:
        signature = b64decode(unquote(signature))
    except TypeError:
        request.errors.add(document_container, 'url', "Document url signature invalid.")
        request.errors.status = 422
        raise error_handler(request.errors)
    mess = "{}\0{}".format(key, document.hash.split(':', 1)[-1])
    try:
        if mess != dockey.verify(signature + mess.encode("utf-8")):
            raise ValueError
    except ValueError:
        request.errors.add(document_container, 'url', "Document url invalid.")
        request.errors.status = 422
        raise error_handler(request.errors)
    document_route = request.matched_route.name.replace("collection_", "")
    if "Documents" not in document_route:
        specified_document_route_end = (document_container.lower().rsplit('documents')[0] + ' documents').lstrip().title()
        document_route = ' '.join([document_route[:-1], specified_document_route_end])
    route_kwargs.update({'_route_name': document_route, 'document_id': document.id, '_query': {'download': key}})
    document_path = request.current_route_path(**route_kwargs)
    document.url = '/' + '/'.join(document_path.split('/')[3:])
    return document


def check_complaint_status(request, complaint, now=None):
    if not now:
        now = get_now()
    if complaint.status == 'claim' and calculate_business_date(complaint.dateSubmitted, COMPLAINT_STAND_STILL_TIME, request.tender) < now:
        complaint.status = 'pending'
        complaint.type = 'complaint'
        complaint.dateEscalated = now
    elif complaint.status == 'answered' and calculate_business_date(complaint.dateAnswered, COMPLAINT_STAND_STILL_TIME, request.tender) < now:
        complaint.status = complaint.resolutionType


def check_status(request):
    tender = request.validated['tender']
    now = get_now()
    for complaint in tender.complaints:
        check_complaint_status(request, complaint, now)
    for award in tender.awards:
        if award.status == 'active' and not any([i.awardID == award.id for i in tender.contracts]):
            tender.contracts.append(type(tender).contracts.model_class({
                'awardID': award.id,
                'suppliers': award.suppliers,
                'value': award.value,
                'date': now,
                'items': [i for i in tender.items if i.relatedLot == award.lotID ],
                'contractID': '{}-{}{}'.format(tender.tenderID, request.registry.server_id, len(tender.contracts) + 1) }))
            add_next_award(request)
        for complaint in award.complaints:
            check_complaint_status(request, complaint, now)
    if tender.status == 'active.enquiries' and not tender.tenderPeriod.startDate and tender.enquiryPeriod.endDate.astimezone(TZ) <= now:
        LOGGER.info('Switched tender {} to {}'.format(tender.id, 'active.tendering'),
                    extra=context_unpack(request, {'MESSAGE_ID': 'switched_tender_active.tendering'}))
        tender.status = 'active.tendering'
        return
    elif tender.status == 'active.enquiries' and tender.tenderPeriod.startDate and tender.tenderPeriod.startDate.astimezone(TZ) <= now:
        LOGGER.info('Switched tender {} to {}'.format(tender.id, 'active.tendering'),
                    extra=context_unpack(request, {'MESSAGE_ID': 'switched_tender_active.tendering'}))
        tender.status = 'active.tendering'
        return
    elif not tender.lots and tender.status == 'active.tendering' and tender.tenderPeriod.endDate <= now:
        LOGGER.info('Switched tender {} to {}'.format(tender['id'], 'active.auction'),
                    extra=context_unpack(request, {'MESSAGE_ID': 'switched_tender_active.auction'}))
        tender.status = 'active.auction'
        remove_draft_bids(request)
        check_bids(request)
        if tender.numberOfBids < 2 and tender.auctionPeriod:
            tender.auctionPeriod.startDate = None
        return
    elif tender.lots and tender.status == 'active.tendering' and tender.tenderPeriod.endDate <= now:
        LOGGER.info('Switched tender {} to {}'.format(tender['id'], 'active.auction'),
                    extra=context_unpack(request, {'MESSAGE_ID': 'switched_tender_active.auction'}))
        tender.status = 'active.auction'
        remove_draft_bids(request)
        check_bids(request)
        [setattr(i.auctionPeriod, 'startDate', None) for i in tender.lots if i.numberOfBids < 2 and i.auctionPeriod]
        return
    elif not tender.lots and tender.status == 'active.awarded':
        standStillEnds = [
            a.complaintPeriod.endDate.astimezone(TZ)
            for a in tender.awards
            if a.complaintPeriod.endDate
        ]
        if not standStillEnds:
            return
        standStillEnd = max(standStillEnds)
        if standStillEnd <= now:
            check_tender_status(request)
    elif tender.lots and tender.status in ['active.qualification', 'active.awarded']:
        if any([i['status'] in tender.block_complaint_status and i.relatedLot is None for i in tender.complaints]):
            return
        for lot in tender.lots:
            if lot['status'] != 'active':
                continue
            lot_awards = [i for i in tender.awards if i.lotID == lot.id]
            standStillEnds = [
                a.complaintPeriod.endDate.astimezone(TZ)
                for a in lot_awards
                if a.complaintPeriod.endDate
            ]
            if not standStillEnds:
                continue
            standStillEnd = max(standStillEnds)
            if standStillEnd <= now:
                check_tender_status(request)
                return


def check_tender_status(request):
    tender = request.validated['tender']
    now = get_now()
    if tender.lots:
        if any([i.status in tender.block_complaint_status and i.relatedLot is None for i in tender.complaints]):
            return
        for lot in tender.lots:
            if lot.status != 'active':
                continue
            lot_awards = [i for i in tender.awards if i.lotID == lot.id]
            if not lot_awards:
                continue
            last_award = lot_awards[-1]
            pending_complaints = any([
                i['status'] in tender.block_complaint_status and i.relatedLot == lot.id
                for i in tender.complaints
            ])
            pending_awards_complaints = any([
                i.status in tender.block_complaint_status
                for a in lot_awards
                for i in a.complaints
            ])
            stand_still_end = max([
                a.complaintPeriod.endDate or now
                for a in lot_awards
            ])
            if pending_complaints or pending_awards_complaints or not stand_still_end <= now:
                continue
            elif last_award.status == 'unsuccessful':
                LOGGER.info('Switched lot {} of tender {} to {}'.format(lot.id, tender.id, 'unsuccessful'),
                            extra=context_unpack(request, {'MESSAGE_ID': 'switched_lot_unsuccessful'}, {'LOT_ID': lot.id}))
                lot.status = 'unsuccessful'
                continue
            elif last_award.status == 'active' and any([i.status == 'active' and i.awardID == last_award.id for i in tender.contracts]):
                LOGGER.info('Switched lot {} of tender {} to {}'.format(lot.id, tender.id, 'complete'),
                            extra=context_unpack(request, {'MESSAGE_ID': 'switched_lot_complete'}, {'LOT_ID': lot.id}))
                lot.status = 'complete'
        statuses = set([lot.status for lot in tender.lots])
        if statuses == set(['cancelled']):
            LOGGER.info('Switched tender {} to {}'.format(tender.id, 'cancelled'),
                        extra=context_unpack(request, {'MESSAGE_ID': 'switched_tender_cancelled'}))
            tender.status = 'cancelled'
        elif not statuses.difference(set(['unsuccessful', 'cancelled'])):
            LOGGER.info('Switched tender {} to {}'.format(tender.id, 'unsuccessful'),
                        extra=context_unpack(request, {'MESSAGE_ID': 'switched_tender_unsuccessful'}))
            tender.status = 'unsuccessful'
        elif not statuses.difference(set(['complete', 'unsuccessful', 'cancelled'])):
            LOGGER.info('Switched tender {} to {}'.format(tender.id, 'complete'),
                        extra=context_unpack(request, {'MESSAGE_ID': 'switched_tender_complete'}))
            tender.status = 'complete'
    else:
        pending_complaints = any([
            i.status in tender.block_complaint_status
            for i in tender.complaints
        ])
        pending_awards_complaints = any([
            i.status in tender.block_complaint_status
            for a in tender.awards
            for i in a.complaints
        ])
        stand_still_ends = [
            a.complaintPeriod.endDate
            for a in tender.awards
            if a.complaintPeriod.endDate
        ]
        stand_still_end = max(stand_still_ends) if stand_still_ends else now
        stand_still_time_expired = stand_still_end < now
        last_award_status = tender.awards[-1].status if tender.awards else ''
        if not pending_complaints and not pending_awards_complaints and stand_still_time_expired and last_award_status == 'unsuccessful':
            LOGGER.info('Switched tender {} to {}'.format(tender.id, 'unsuccessful'),
                        extra=context_unpack(request, {'MESSAGE_ID': 'switched_tender_unsuccessful'}))
            tender.status = 'unsuccessful'
        if tender.contracts and tender.contracts[-1].status == 'active':
            tender.status = 'complete'


def add_next_award(request):
    tender = request.validated['tender']
    now = get_now()
    if not tender.awardPeriod:
        tender.awardPeriod = type(tender).awardPeriod({})
    if not tender.awardPeriod.startDate:
        tender.awardPeriod.startDate = now
    if tender.lots:
        statuses = set()
        for lot in tender.lots:
            if lot.status != 'active':
                continue
            lot_awards = [i for i in tender.awards if i.lotID == lot.id]
            if lot_awards and lot_awards[-1].status in ['pending', 'active']:
                statuses.add(lot_awards[-1].status if lot_awards else 'unsuccessful')
                continue
            lot_items = [i.id for i in tender.items if i.relatedLot == lot.id]
            features = [
                i
                for i in (tender.features or [])
                if i.featureOf == 'tenderer' or i.featureOf == 'lot' and i.relatedItem == lot.id or i.featureOf == 'item' and i.relatedItem in lot_items
            ]
            codes = [i.code for i in features]
            bids = [
                {
                    'id': bid.id,
                    'value': [i for i in bid.lotValues if lot.id == i.relatedLot][0].value,
                    'tenderers': bid.tenderers,
                    'parameters': [i for i in bid.parameters if i.code in codes],
                    'date': [i for i in bid.lotValues if lot.id == i.relatedLot][0].date
                }
                for bid in tender.bids
                if lot.id in [i.relatedLot for i in bid.lotValues]
            ]
            if not bids:
                lot.status = 'unsuccessful'
                statuses.add('unsuccessful')
                continue
            unsuccessful_awards = [i.bid_id for i in lot_awards if i.status == 'unsuccessful']
            bids = chef(bids, features, unsuccessful_awards)
            if bids:
                bid = bids[0]
                award = type(tender).awards.model_class({
                    'bid_id': bid['id'],
                    'lotID': lot.id,
                    'status': 'pending',
                    'value': bid['value'],
                    'date': get_now(),
                    'suppliers': bid['tenderers'],
                    'complaintPeriod': {
                        'startDate': now.isoformat()
                    }
                })
                tender.awards.append(award)
                request.response.headers['Location'] = request.route_url('{}:Tender Awards'.format(tender.procurementMethodType), tender_id=tender.id, award_id=award['id'])
                statuses.add('pending')
            else:
                statuses.add('unsuccessful')
        if statuses.difference(set(['unsuccessful', 'active'])):
            tender.awardPeriod.endDate = None
            tender.status = 'active.qualification'
        else:
            tender.awardPeriod.endDate = now
            tender.status = 'active.awarded'
    else:
        if not tender.awards or tender.awards[-1].status not in ['pending', 'active']:
            unsuccessful_awards = [i.bid_id for i in tender.awards if i.status == 'unsuccessful']
            bids = chef(tender.bids, tender.features or [], unsuccessful_awards)
            if bids:
                bid = bids[0].serialize()
                award = type(tender).awards.model_class({
                    'bid_id': bid['id'],
                    'status': 'pending',
                    'date': get_now(),
                    'value': bid['value'],
                    'suppliers': bid['tenderers'],
                    'complaintPeriod': {
                        'startDate': get_now().isoformat()
                    }
                })
                tender.awards.append(award)
                request.response.headers['Location'] = request.route_url('{}:Tender Awards'.format(tender.procurementMethodType), tender_id=tender.id, award_id=award['id'])
        if tender.awards[-1].status == 'pending':
            tender.awardPeriod.endDate = None
            tender.status = 'active.qualification'
        else:
            tender.awardPeriod.endDate = now
            tender.status = 'active.awarded'
