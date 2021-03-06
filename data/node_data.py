from env import DB_CONN_PARAMETER_WI
import numpy as np

from sqlalchemy import create_engine, desc, or_, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import base, func

from AutoMap import *
from env import KARIM_API_KEY
from datetime import datetime

import googlemaps


def get_main_pflow(scenario_id, baseline_id, pdct_fam, session):

    model_pflows = session.query(ScenarioLanes.pflow).filter(
        ScenarioLanes.scenario_id == scenario_id,
        ScenarioLanes.baseline_id == baseline_id,
        ScenarioLanes.parent_pflow == None, 
        ScenarioLanes.pdct_fam == pdct_fam, 
        ScenarioLanes.in_pflow == 1
        ).distinct()
    return model_pflows

def populate_Locations(session):
    stmt = text("""
            insert into scdsi_locations (name, region)
            select distinct lower(ship_from_name), lower(ship_from_region_code)
            from scdsi_cv_lane_rate_automation_pl 
            where billed_weight != 0 
            and shipment_type not in ('OTHER', 'BROKERAGE')
        """)

    session.execute(stmt)
    session.commit()

    stmt = text("""
            insert into scdsi_locations (name, region)
            select distinct lower(ship_to_name), lower(ship_to_region_code)
            from scdsi_cv_lane_rate_automation_pl 
            where billed_weight != 0 
            and shipment_type in ('LEG2-2')
    """)

    session.execute(stmt)
    session.commit()

def get_lat_long(session, node=None):
    gm = googlemaps.Client(key=KARIM_API_KEY)

    if not node:
        locations = session.query(Locations).filter(Locations.lat == None).all()#.limit(2000)
    else:
        locations = [node]

    i = 0

    start = datetime.now()

    for location in locations:
        name = location.name
        try:
            api = gm.geocode(name)
            location.lat = api[0]["geometry"]["location"]["lat"]
            location.long = api[0]["geometry"]["location"]["lng"]
        except:
            api = None
        # print(api)
        if i % 50 == 0:
            session.commit()
        if i % 100 == 0:
            print(i, '-', datetime.now() - start)
        
        i += 1

def populate_baseline_nodes(baseline_id, session):
    stmt = text("""
        insert into scdsi_scenario_nodes 
        (baseline_id, scenario_id, pdct_fam, name, region, role, pflow, in_pflow, total_alpha)
        select baseline_id, scenario_id, pdct_fam, ori_name, ori_region, ori_role,
        case 
            when parent_pflow is null then pflow
            else parent_pflow 
        end as composite_pflow, 1, 
        case
            when sum(total_alpha) > 1 then 1
            else sum(total_alpha)
        end
        from scdsi_scenario_lanes
        where in_pflow = 1
        and baseline_id = baseline_id
        and scenario_id = 0
        group by baseline_id, scenario_id, pdct_fam, ori_name, ori_region, ori_role, composite_pflow, in_pflow
    """).params(baseline_id = baseline_id)

    session.execute(stmt)
    session.commit()

    stmt = text("""
        insert into scdsi_scenario_nodes 
        (baseline_id, scenario_id, pdct_fam, name, region, role, pflow, in_pflow, total_alpha)
        select distinct baseline_id, scenario_id, pdct_fam, desti_name, desti_region, desti_role,
        case 
            when parent_pflow is null then pflow
            else parent_pflow 
        end as pflow, 1, -1
        from scdsi_scenario_lanes
        where in_pflow = 1 
        and desti_role = 'Customer'
        and baseline_id = baseline_id
        and scenario_id = 0
    """).params(baseline_id = baseline_id)

    session.execute(stmt)
    session.commit()

def get_node_supply(scenario_id, baseline_id, pdct_fam, session):
    nodes = session.query(ScenarioNodes).filter(
        ScenarioNodes.scenario_id == scenario_id,
        ScenarioNodes.baseline_id == baseline_id,
        ScenarioNodes.pdct_fam == pdct_fam,
        ScenarioNodes.supply is not None
        )

    for n in nodes.all():
        if n.role == 'Customer':
            total = session.query(func.sum(ScenarioLanes.total_weight).label('total')).filter(
                ScenarioLanes.scenario_id == n.scenario_id,
                ScenarioLanes.baseline_id == n.baseline_id,
                ScenarioLanes.desti_name == n.name,
                # ScenarioLanes.desti_country == n.country, 
                ScenarioLanes.desti_region == n.region, 
                ScenarioLanes.desti_role == n.role,
                ScenarioLanes.pdct_fam == n.pdct_fam,
                ScenarioLanes.in_pflow == 1
                ).group_by(
                    ScenarioLanes.desti_name,
                    # ScenarioLanes.desti_country,
                    ScenarioLanes.desti_region,
                    ScenarioLanes.desti_role
                ).first()
            n.supply = -total.total
        else:
            n.supply = 0
    
    session.commit()

def get_pflow_demand(scenario_id, baseline_id, pdct_fam, session):
    pflow_demand = session.query(
            ScenarioNodes.pdct_fam,
            ScenarioNodes.pflow,
            func.sum(ScenarioNodes.supply).label('total_supply')).filter(
                ScenarioNodes.scenario_id == scenario_id,
                ScenarioNodes.baseline_id == baseline_id,
                ScenarioNodes.pdct_fam == pdct_fam
            ).group_by(
                ScenarioNodes.pflow,
                ScenarioNodes.pdct_fam
                )
    return pflow_demand.all()

def get_node_capacity(scenario_id, baseline_id, pdct_fam, session):
    pflow_demand = get_pflow_demand(scenario_id, baseline_id, pdct_fam, session)

    for p in pflow_demand:
        demand = - p.total_supply
        nodes = session.query(ScenarioNodes).filter(
            ScenarioNodes.scenario_id == scenario_id,
            ScenarioNodes.baseline_id == baseline_id,
            ScenarioNodes.pdct_fam == pdct_fam,
            ScenarioNodes.pflow == p.pflow,
            ScenarioNodes.role.in_(['PCBA', 'DF', 'GHUB', 'OSLC', 'DSLC'])
        ).all()
        for node in nodes:
            total_alpha = 1 if node.total_alpha == None else node.total_alpha
            node.capacity = demand * total_alpha / 0.8
        # stmt = text("""
        #     update scdsi_scenario_nodes 
        #     set capacity = sub.cap
        #     from (
        #         select total_alpha * :demand / 0.8 as cap, ori_role, ori_name, ori_region
        #         from scdsi_scenario_lanes join scdsi_scenario_nodes
        #         on ori_role = role and ori_name = name and ori_region = region 
        #         and scdsi_scenario_lanes.scenario_id = scdsi_scenario_nodes.scenario_id and scdsi_scenario_lanes.baseline_id = scdsi_scenario_nodes.baseline_id
        #         where ori_role in ('PCBA', 'DF', 'GHUB', 'OSLC', 'DSLC')
        #         and scdsi_scenario_nodes.pflow = :pflow
        #     ) as sub
        #     where role = sub.ori_role and name = sub.ori_name and sub.ori_region = region
        #     and scenario_id = :scenario_id and baseline_id = :baseline_id and pflow = :pflow
        # """).params(
        #         demand = demand, 
        #         pflow = p.pflow, 
        #         scenario_id = scenario_id, 
        #         baseline_id = baseline_id
        #     )
        # session.execute(stmt)

        stmt = text("""
            update scdsi_scenario_nodes
            set capacity = -1
            where role in ('Customer', 'Supplier', 'Gateway')
        """)
        session.execute(stmt)
    session.commit()





if __name__ == '__main__':
    engine = create_engine(DB_CONN_PARAMETER_CLOUD)
    Session = sessionmaker(bind=engine)
    session = Session()

    baseline_id = 4
    scenario_id = 0

    pdct_fam = 'AIRANT'

    start = datetime.now()
    print(start)

    # get_node_supply('4400ISR', session)
    # get_node_capacity('4400ISR', session)

    # populate_Locations(session)
    # get_lat_long(session)

    populate_baseline_nodes(baseline_id, session)

    # pdct_fams = session.query(ScenarioLanes.pdct_fam).distinct().all()
    pdct_fams = [('AIRANT',), ('WPHONE',), ('SBPHONE',), ('PHONVOC',)]
    pdct_fams = [('QSFP40G',)]
    pdct_fams = [('AIRANT',), ('C2960X',), ('4400ISR',), ('WPHONE',), ('SBPHONE',), ('PHONE',)]

    for pdct_fam in pdct_fams:
        print(pdct_fam[0])
        get_node_supply(0, baseline_id, pdct_fam[0], session)
        get_node_capacity(0, baseline_id, pdct_fam[0], session)

    print(datetime.now() - start)
    session.commit()